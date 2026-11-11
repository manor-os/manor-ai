import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import StepGraph from "./StepGraph";
import type { StepNode } from "./StepGraph";
import LoadingSpinner from "./LoadingSpinner";
import EmptyState from "./EmptyState";
import { t } from "../../lib/i18n";
import { goalIdentityDedupeKey } from "../../lib/goalIdentity";
import { formatGoalNumber, goalProgressPercent } from "../../lib/goalNumbers";

/**
 * Workspace-level goal wire graph (canvas with pan/zoom).
 * Shows current goals and tasks as a unified DAG. Goals connect to their
 * linked tasks, and dependent tasks wait for their upstream tasks.
 * Click a node to see details in the side panel.
 */

interface WorkspaceGoalGraphProps {
  workspaceId: string;
}

function normalizeGraphStatus(status?: string | null): string {
  const raw = String(status || "pending").toLowerCase();
  if (raw === "done") return "completed";
  if (raw === "in_progress") return "running";
  if (raw === "waiting_human" || raw === "waiting_on_customer" || raw === "pending_approval" || raw === "blocked") return "waiting";
  if (raw === "cancelled" || raw === "canceled" || raw === "skipped" || raw === "proposed" || raw === "queued" || raw === "draft" || raw === "missing") return "pending";
  return raw;
}

function goalGraphStatus(goal: any): string {
  const status = String(goal?.status || "active").toLowerCase();
  const pace = String(goal?.pace_status || "").toLowerCase();
  if (status === "achieved" || pace === "achieved") return "completed";
  if (status === "paused" || status === "blocked" || status === "pending_approval") return "waiting";
  if (status === "failed" || status === "cancelled") return "failed";
  if (status === "active") return "running";
  return normalizeGraphStatus(status);
}

function goalDisplayPace(goal: any): string | undefined {
  const pace = String(goal?.pace_status || "").toLowerCase();
  const status = String(goal?.status || "").toLowerCase();
  if (pace && pace !== "unknown") return pace;
  if (status === "achieved") return "achieved";
  if (status === "paused") return "paused";
  if (status === "active") return "tracking";
  return undefined;
}

function hasMeasuredGoalValue(goal: any): boolean {
  return goal?.current_value !== null && goal?.current_value !== undefined && goal?.current_value !== "";
}

function formatGoalValue(value: unknown): string {
  return formatGoalNumber(value) ?? t("page.workspace_detail.not_measured_yet");
}

function goalOutcomeLabel(goal: any, linkedStat?: { name?: string | null }): string {
  const target = `${t("page.workspace_detail.target")} ${formatGoalValue(goal?.target_value)}`;
  if (hasMeasuredGoalValue(goal)) {
    return `${formatGoalValue(goal.current_value)} / ${formatGoalValue(goal.target_value)}`;
  }

  const provider = String(goal?.measurement_source?.provider || "").trim().toLowerCase();
  if (provider === "manual") return `${t("component.workspace_goal_graph.manual_measurement_needed")} · ${target}`;
  if (provider === "workspace_internal") return `${t("page.workspace_detail.auto_measuring_workspace")} · ${target}`;
  if (!provider && linkedStat?.name) return `${linkedStat.name} · ${target}`;
  if (!provider) return `${t("component.workspace_goal_graph.no_measurement_source")} · ${target}`;
  return `${t("page.workspace_detail.not_measured_yet")} · ${target}`;
}

function goalStatusTone(goal: any): { bg: string; fg: string; border: string; bar: string } {
  const status = String(goal?.status || "active").toLowerCase();
  const pace = String(goal?.pace_status || "").toLowerCase();
  if (status === "achieved" || pace === "achieved") {
    return { bg: "#f1f6f3", fg: "#3f7361", border: "#c4dfd2", bar: "#4f9c84" };
  }
  if (status === "paused" || status === "blocked" || pace === "behind") {
    return { bg: "#faf7ef", fg: "#76502c", border: "#ecdca4", bar: "#cf9b44" };
  }
  if (status === "failed" || status === "cancelled" || pace === "at_risk") {
    return { bg: "#f8f0ef", fg: "#a23e38", border: "#ecc8c5", bar: "#d65f59" };
  }
  return { bg: "#f1f6f5", fg: "#436b65", border: "#ccded9", bar: "#5f928a" };
}

function goalStatusLabel(goal: any): string {
  const status = String(goal?.status || "active").toLowerCase();
  const pace = goalDisplayPace(goal);
  const paceLabel = pace === "tracking" ? t("page.workspace_detail.tracking") : pace ? pace.replace(/_/g, " ") : "";
  return paceLabel && paceLabel !== status ? `${status} · ${paceLabel}` : status;
}

function statusCount(counts: Record<string, unknown>, keys: string[]): number {
  return keys.reduce((sum, key) => sum + Number(counts?.[key] || 0), 0);
}

function pluralizeTask(count: number): string {
  return count === 1
    ? t("component.workspace_goal_graph.task")
    : t("component.workspace_goal_graph.task_plural");
}

const HIDDEN_GOAL_STATUSES = new Set(["paused", "abandoned", "cancelled", "archived", "deleted"]);

function goalGraphCompletenessScore(goal: any): number {
  let score = 0;
  if (goal?.status === "active") score += 16;
  if (goal?.current_value !== null && goal?.current_value !== undefined) score += 8;
  if (goal?.target_value !== null && goal?.target_value !== undefined && Number(goal.target_value) !== 0) score += 4;
  if (goal?.measurement_source) score += 2;
  if (goal?.description) score += 1;
  return score;
}

function visibleGraphGoals(rawGoals: any[]): any[] {
  const byKey = new Map<string, any>();
  for (const goal of rawGoals || []) {
    const status = String(goal?.status || "active").toLowerCase();
    if (HIDDEN_GOAL_STATUSES.has(status)) continue;
    const key = goalIdentityDedupeKey(goal);
    const existing = byKey.get(key);
    if (!existing || goalGraphCompletenessScore(goal) > goalGraphCompletenessScore(existing)) {
      byKey.set(key, goal);
    }
  }
  return Array.from(byKey.values());
}

function uniqueStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return Array.from(new Set(value.map((item) => String(item || "").trim()).filter(Boolean)));
}

function taskDependencyIds(task: any): string[] {
  const details = task?.details || {};
  return uniqueStringList(details.depends_on_task_ids);
}

function taskGoalIds(task: any): string[] {
  const details = task?.details || {};
  return uniqueStringList(details.goal_ids || details.linked_goal_ids);
}

function taskNodeId(taskId: string): string {
  return `task-${taskId}`;
}

const TERMINAL_TASK_STATUSES = new Set(["completed", "failed", "cancelled", "canceled"]);

function taskSortTime(task: any): number {
  const raw = task?.updated_at || task?.completed_at || task?.created_at || "";
  const time = Date.parse(raw);
  return Number.isFinite(time) ? time : 0;
}

export default function WorkspaceGoalGraph({ workspaceId }: WorkspaceGoalGraphProps) {
  const [selectedNode, setSelectedNode] = useState<StepNode | null>(null);
  const [isCompact, setIsCompact] = useState(false);

  useEffect(() => {
    const update = () => setIsCompact(window.innerWidth < 760);
    update();
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);

  // Fetch goals for workspace
  const { data: goalsRaw, isLoading: goalsLoading } = useQuery({
    queryKey: ["workspace-goals-graph", workspaceId],
    queryFn: () => api.goals.list({ workspace_id: workspaceId, limit: 50 }),
    enabled: !!workspaceId,
  });

  // Stat-bound Goals keep their measurement source in the local WorkspaceStat
  // definition. Load that definition so the graph does not mislabel a valid
  // stat binding as an unconfigured Goal.
  const { data: statsRaw, isLoading: statsLoading } = useQuery({
    queryKey: ["workspace-stats", workspaceId],
    queryFn: () => api.workspaces.stats.list(workspaceId),
    enabled: !!workspaceId,
  });

  // Fetch tasks so the canvas can show the real hierarchy:
  // Goal -> Task, plus task-to-task dependencies.
  const { data: tasksPage, isLoading: tasksLoading } = useQuery({
    queryKey: ["workspace-goal-graph-tasks", workspaceId],
    queryFn: () => api.tasks.list({ workspace_id: workspaceId, limit: 200 }),
    enabled: !!workspaceId,
  });

  const isLoading = goalsLoading || statsLoading || tasksLoading;

  if (isLoading) {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: "48px 0" }}>
        <LoadingSpinner size={28} />
      </div>
    );
  }

  const rawGoalsList: any[] = Array.isArray(goalsRaw) ? goalsRaw : (goalsRaw as any)?.items ?? [];
  const goalsList = visibleGraphGoals(rawGoalsList);
  const statsList: any[] = (statsRaw as any)?.items || [];
  const statsById = new Map(statsList.map((stat: any) => [String(stat.id), stat]));
  const rawTasksList: any[] = Array.isArray(tasksPage) ? tasksPage : (tasksPage as any)?.items ?? [];

  // Build unified graph nodes
  const graphNodes: StepNode[] = [];
  const nodeMetadata = new Map<string, any>();
  const taskGoalNodeIds = new Map<string, string[]>();
  const goalNodeIdByGoalId = new Map<string, string>();
  const taskById = new Map<string, any>();

  for (const task of rawTasksList) {
    if (task?.id) taskById.set(String(task.id), task);
  }
  for (const goal of goalsList) {
    const goalNodeId = `goal-${goal.id}`;
    goalNodeIdByGoalId.set(String(goal.id), goalNodeId);
    graphNodes.push({
      id: goalNodeId,
      name: goal.title,
      status: goalGraphStatus(goal),
      type: "goal",
      depends_on: [],
    });
    nodeMetadata.set(goalNodeId, {
      kind: "goal",
      description: goal.description,
      metric_key: goal.metric_key,
      current_value: goal.current_value,
      target_value: goal.target_value,
      pace_status: goalDisplayPace(goal),
      deadline: goal.deadline,
      linked_task_ids: goal.linked_task_ids || [],
      task_status_counts: goal.task_status_counts || {},
      task_progress_fraction: goal.task_progress_fraction,
      estimated_impact_total: goal.estimated_impact_total,
      actual_impact_total: goal.actual_impact_total,
    });
    for (const taskId of goal.linked_task_ids || []) {
      if (!taskId) continue;
      const existing = taskGoalNodeIds.get(taskId) || [];
      existing.push(goalNodeId);
      taskGoalNodeIds.set(taskId, existing);
    }
  }

  for (const task of taskById.values()) {
    for (const goalId of taskGoalIds(task)) {
      const goalNodeId = goalNodeIdByGoalId.get(goalId);
      if (!goalNodeId || !task?.id) continue;
      const taskId = String(task.id);
      const existing = taskGoalNodeIds.get(taskId) || [];
      if (!existing.includes(goalNodeId)) existing.push(goalNodeId);
      taskGoalNodeIds.set(taskId, existing);
    }
  }

  const graphTaskIds = new Set<string>();
  for (const task of rawTasksList) {
    if (!task?.id) continue;
    const taskStatus = String(task.status || "").toLowerCase();
    if (taskDependencyIds(task).length > 0 || !TERMINAL_TASK_STATUSES.has(taskStatus)) {
      graphTaskIds.add(String(task.id));
    }
  }
  for (const taskId of taskGoalNodeIds.keys()) {
    graphTaskIds.add(String(taskId));
    if (!taskById.has(String(taskId))) {
      taskById.set(String(taskId), {
        id: String(taskId),
        title: `Task ${String(taskId).slice(-6)}`,
        status: "pending",
        details: {},
      });
    }
  }

  // Include upstream dependency tasks recursively so the DAG keeps its real
  // execution order even when the dependency task is not linked to a goal.
  const pendingTaskIds = [...graphTaskIds];
  while (pendingTaskIds.length > 0) {
    const currentTaskId = pendingTaskIds.pop()!;
    const currentTask = taskById.get(currentTaskId);
    for (const depTaskId of taskDependencyIds(currentTask)) {
      if (!taskById.has(depTaskId)) {
        taskById.set(depTaskId, {
          id: depTaskId,
          title: `Dependency ${depTaskId.slice(-6)}`,
          status: "pending",
          details: {},
        });
      }
      if (!graphTaskIds.has(depTaskId)) {
        graphTaskIds.add(depTaskId);
        pendingTaskIds.push(depTaskId);
      }
    }
  }

  if (goalsList.length === 0 && graphTaskIds.size === 0) {
    return (
      <EmptyState
        title={t("component.workspace_goal_graph.no_active_execution")}
        description={t("component.workspace_goal_graph.execution_graph_empty_desc")}
      />
    );
  }

  const taskNodeByTaskId = new Map<string, StepNode>();
  const orderedTaskIds = [...graphTaskIds].sort(
    (a, b) => taskSortTime(taskById.get(a)) - taskSortTime(taskById.get(b)),
  );

  for (const taskId of orderedTaskIds) {
    const task = taskById.get(taskId);
    const goalNodeIds = taskGoalNodeIds.get(taskId) || [];
    const taskNode: StepNode = {
      id: taskNodeId(taskId),
      name: task?.title || "Task",
      status: normalizeGraphStatus(task?.status || "pending"),
      type: "task",
      depends_on: [...goalNodeIds],
    };
    taskNodeByTaskId.set(taskId, taskNode);
    graphNodes.push(taskNode);
    nodeMetadata.set(taskNode.id, {
      kind: "task",
      description: task?.description,
      task_id: taskId,
      task_status: task?.status,
      priority: task?.priority,
      dependency_task_ids: taskDependencyIds(task),
      linked_goal_ids: taskGoalIds(task),
    });
  }

  for (const [taskId, taskNode] of taskNodeByTaskId.entries()) {
    const task = taskById.get(taskId);
    const deps = new Set(taskNode.depends_on || []);
    for (const depTaskId of taskDependencyIds(task)) {
      if (taskNodeByTaskId.has(depTaskId)) deps.add(taskNodeId(depTaskId));
    }
    deps.delete(taskNode.id);
    taskNode.depends_on = [...deps];
  }

  const handleNodeClick = (node: StepNode) => {
    setSelectedNode(selectedNode?.id === node.id ? null : node);
  };

  const linkedGoalTaskCount = taskGoalNodeIds.size;
  const graphStats = [
    { label: t("component.workspace_goal_graph.goals"), value: goalsList.length },
    { label: t("component.workspace_goal_graph.linked_tasks"), value: linkedGoalTaskCount },
    { label: t("component.workspace_goal_graph.tasks"), value: graphTaskIds.size },
  ];

  return (
    <div className="workspace-goal-graph" style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <ExecutionMapPanel
        graphNodes={graphNodes}
        graphStats={graphStats}
        isCompact={isCompact}
        selectedNode={selectedNode}
        selectedMetadata={selectedNode ? nodeMetadata.get(selectedNode.id) : undefined}
        onNodeClick={handleNodeClick}
        onCloseDetail={() => setSelectedNode(null)}
        height={isCompact ? 420 : 520}
      />

      <GoalStatusOverview
        goals={goalsList}
        statsById={statsById}
        isCompact={isCompact}
        selectedGoalId={selectedNode?.type === "goal" ? selectedNode.id : undefined}
        onSelectGoal={(goal) => {
          const node = graphNodes.find((candidate) => candidate.id === `goal-${goal.id}`);
          if (node) setSelectedNode(selectedNode?.id === node.id ? null : node);
        }}
      />
    </div>
  );
}

function ExecutionMapPanel({
  graphNodes,
  graphStats,
  isCompact,
  selectedNode,
  selectedMetadata,
  onNodeClick,
  onCloseDetail,
  height,
}: {
  graphNodes: StepNode[];
  graphStats: { label: string; value: number | string }[];
  isCompact: boolean;
  selectedNode: StepNode | null;
  selectedMetadata?: any;
  onNodeClick: (node: StepNode) => void;
  onCloseDetail: () => void;
  height: number;
}) {
  return (
    <section className="workspace-goal-map-panel" style={{
      border: "0.5px solid rgba(29, 29, 31,0.06)",
      borderRadius: 18,
      background: "rgba(255,255,255,0.9)",
      overflow: "hidden",
      boxShadow: "0 16px 42px rgba(29, 29, 31,0.06)",
    }}>
      <div className="workspace-goal-map-header" style={{
        display: "flex",
        alignItems: "flex-start",
        justifyContent: "space-between",
        gap: 16,
        padding: "16px 18px",
        borderBottom: "0.5px solid rgba(29, 29, 31,0.06)",
        background: "linear-gradient(180deg, rgba(250,250,249,0.96), rgba(255,255,255,0.96))",
        flexWrap: "wrap",
      }}>
        <div style={{ minWidth: 220, flex: "1 1 280px" }}>
          <div className="workspace-goal-map-title" style={{ fontSize: 12, fontWeight: 850, color: "#436b65", }}>
            {t("component.workspace_goal_graph.execution_map")}
          </div>
          <div className="workspace-goal-map-description" style={{ marginTop: 4, fontSize: 12.5, lineHeight: 1.5, color: "var(--text-muted)" }}>
            {t("component.workspace_goal_graph.execution_map_desc")}
          </div>
        </div>
        <div className="workspace-goal-map-stats" style={{ display: "flex", flexWrap: "wrap", justifyContent: isCompact ? "flex-start" : "flex-end", gap: 8 }}>
          {graphStats.map((stat) => (
            <GraphStat key={stat.label} label={stat.label} value={stat.value} />
          ))}
        </div>
      </div>

      <div className="workspace-goal-map-body" style={{ padding: isCompact ? 12 : 14 }}>
        <div style={{
          display: "grid",
          gridTemplateColumns: selectedNode && !isCompact ? "minmax(0, 1fr) 310px" : "minmax(0, 1fr)",
          gap: 14,
          alignItems: "start",
        }}>
          <div style={{ minWidth: 0 }}>
            <div style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 12,
              marginBottom: 10,
              flexWrap: "wrap",
            }}>
              <div className="workspace-goal-map-legend-row" style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                <Legend color="#9079c2" label={t("component.workspace_goal_graph.goal")} />
                <Legend color="#34d399" label={t("component.status.completed")} />
                <Legend color="#8aa9d1" label={t("component.status.running")} />
                <Legend color="#ddbb63" label={t("component.status.waiting")} />
                <Legend color="#e5e5ea" label={t("component.status.pending")} />
                <Legend color="#d18b86" label={t("component.status.failed")} />
              </div>
              <span className="workspace-goal-map-hint" style={{ fontSize: 10.5, color: "var(--text-muted)" }}>
                {t("component.workspace_goal_graph.canvas_hint")}
              </span>
            </div>

            {selectedNode && isCompact && (
              <div style={{ marginBottom: 12 }}>
                <NodeDetailPanel
                  node={selectedNode}
                  metadata={selectedMetadata}
                  onClose={onCloseDetail}
                  compact
                />
              </div>
            )}

            <StepGraph
              steps={graphNodes}
              height={height}
              variant="goal"
              onStepClick={onNodeClick}
              activeStepId={selectedNode?.id}
            />
          </div>

          {selectedNode && !isCompact && (
            <NodeDetailPanel
              node={selectedNode}
              metadata={selectedMetadata}
              onClose={onCloseDetail}
            />
          )}
        </div>
      </div>
    </section>
  );
}

function GraphStat({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="workspace-goal-map-stat" style={{
      minWidth: 76,
      border: "0.5px solid rgba(29, 29, 31,0.06)",
      borderRadius: 12,
      padding: "7px 10px",
      background: "var(--surface-panel)",
      boxShadow: "0 8px 18px rgba(29, 29, 31,0.04)",
    }}>
      <div className="workspace-goal-map-stat-label" style={{ fontSize: 9.5, fontWeight: 800, color: "var(--text-muted)", }}>
        {label}
      </div>
      <div className="workspace-goal-map-stat-value" style={{ marginTop: 2, fontSize: 15, fontWeight: 850, color: "var(--text-strong)" }}>
        {value}
      </div>
    </div>
  );
}

function GoalStatusOverview({
  goals,
  statsById,
  isCompact,
  selectedGoalId,
  onSelectGoal,
}: {
  goals: any[];
  statsById: Map<string, any>;
  isCompact: boolean;
  selectedGoalId?: string;
  onSelectGoal: (goal: any) => void;
}) {
  if (!goals.length) return null;

  return (
    <section className="workspace-goal-overview" style={{
      border: "0.5px solid rgba(29, 29, 31,0.06)",
      borderRadius: 18,
      background: "rgba(255,255,255,0.86)",
      padding: isCompact ? 12 : 14,
      boxShadow: "0 14px 36px rgba(29, 29, 31,0.045)",
    }}>
      <div style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 10,
        marginBottom: 10,
      }}>
        <div className="workspace-goal-overview-title" style={{ fontSize: 11, fontWeight: 800, color: "var(--text-muted)", }}>
          {t("component.workspace_goal_graph.goal_overview")} · {goals.length}
        </div>
      </div>

      <div style={{
        display: "grid",
        gridTemplateColumns: isCompact ? "1fr" : "repeat(auto-fit, minmax(220px, 1fr))",
        gap: 10,
      }}>
        {goals.map((goal) => {
          const nodeId = `goal-${goal.id}`;
          const selected = selectedGoalId === nodeId;
          const linkedStat = goal.stat_id ? statsById.get(String(goal.stat_id)) : undefined;
          const tone = goalStatusTone(goal);
          const measured = hasMeasuredGoalValue(goal);
          const outcomeProgress = measured ? goalProgressPercent(goal) : 0;
          const completionPercent = Math.round(outcomeProgress);
          const linkedTaskCount = Array.isArray(goal.linked_task_ids) ? goal.linked_task_ids.length : 0;
          const counts = goal.task_status_counts || {};
          const completed = statusCount(counts, ["completed"]);
          const running = statusCount(counts, ["in_progress", "running"]);
          const waiting = statusCount(counts, ["waiting", "waiting_human", "waiting_on_customer", "pending_approval", "blocked"]);
          const pending = statusCount(counts, ["pending", "proposed", "draft", "queued", "missing"]);
          const failed = statusCount(counts, ["failed", "error", "cancelled", "canceled"]);
          const taskStatusParts = [
            completed > 0 ? `${completed} ${t("component.status.completed")}` : "",
            running > 0 ? `${running} ${t("component.status.running")}` : "",
            waiting > 0 ? `${waiting} ${t("component.status.waiting")}` : "",
            pending > 0 ? `${pending} ${t("component.status.pending")}` : "",
            failed > 0 ? `${failed} ${t("component.status.failed")}` : "",
          ].filter(Boolean);
          const executionLabel = linkedTaskCount > 0
            ? `${completed}/${linkedTaskCount} ${pluralizeTask(linkedTaskCount)} ${t("component.workspace_goal_graph.complete")}`
            : t("component.workspace_goal_graph.no_linked_tasks");

          return (
            <button
              className={`workspace-goal-overview-card${selected ? " is-selected" : ""}`}
              key={goal.id}
              type="button"
              onClick={() => onSelectGoal(goal)}
              style={{
                textAlign: "left",
                border: selected ? "1px solid #436b65" : "1px solid rgba(214,211,209,0.74)",
                borderRadius: 12,
                padding: 12,
                background: selected
                  ? "linear-gradient(135deg, rgba(242,246,245,0.96), rgba(255,255,255,0.98))"
                  : "#fff",
                boxShadow: selected ? "0 12px 30px rgba(67,107,101,0.14)" : "0 8px 20px rgba(29, 29, 31,0.04)",
                cursor: "pointer",
              }}
            >
              <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 10, marginBottom: 9 }}>
                <div className="workspace-goal-overview-card-title" style={{
                  minWidth: 0,
                  fontSize: 12,
                  fontWeight: 800,
                  color: "var(--text-strong)",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}>
                  {goal.title}
                </div>
                <span className="workspace-goal-overview-status" style={{
                  flexShrink: 0,
                  fontSize: 9,
                  fontWeight: 800,
                  color: tone.fg,
                  background: tone.bg,
                  border: `1px solid ${tone.border}`,
                  borderRadius: 999,
                  padding: "2px 7px",
                }}>
                  {goalStatusLabel(goal)}
                </span>
              </div>

              <div style={{ display: "flex", justifyContent: "space-between", gap: 10, marginBottom: 4 }}>
                <span className="workspace-goal-overview-label" style={{ fontSize: 10, fontWeight: 800, color: "var(--text-muted)", }}>
                  {t("component.workspace_goal_graph.progress")}
                </span>
                <span className="workspace-goal-overview-percent" style={{ fontSize: 10.5, fontWeight: 800, color: measured ? tone.fg : "#aeaeb2" }}>
                  {completionPercent}%
                </span>
              </div>
              <div className="workspace-goal-overview-track" style={{ height: 5, borderRadius: 999, background: "#e5e5ea", overflow: "hidden", marginBottom: 9 }}>
                <div style={{
                  width: `${outcomeProgress}%`,
                  height: "100%",
                  borderRadius: 999,
                  background: measured ? tone.bar : "#d1d1d6",
                  transition: "width 0.45s ease",
                }} />
              </div>
              <div className="workspace-goal-overview-outcome" style={{ fontSize: 10.5, color: "var(--text-muted)", marginBottom: 9 }}>
                {goalOutcomeLabel(goal, linkedStat)}
              </div>

              <div style={{ display: "flex", justifyContent: "space-between", gap: 10, marginBottom: 4 }}>
                <span className="workspace-goal-overview-label" style={{ fontSize: 10, fontWeight: 800, color: "var(--text-muted)", }}>
                  {t("component.workspace_goal_graph.execution")}
                </span>
                <span className="workspace-goal-overview-execution" style={{ fontSize: 10.5, fontWeight: 700, color: linkedTaskCount > 0 ? "#436b65" : "#aeaeb2" }}>
                  {executionLabel}
                </span>
              </div>

              {taskStatusParts.length > 0 && (
                <div style={{ marginTop: 7, display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
                  <span className="workspace-goal-overview-status-parts" style={{ fontSize: 10, color: "var(--text-muted)" }}>
                    {taskStatusParts.join(" · ")}
                  </span>
                </div>
              )}
            </button>
          );
        })}
      </div>
    </section>
  );
}

/* ── Node Detail Panel ───────────────────────────────── */

function NodeDetailPanel({
  node,
  metadata,
  onClose,
  compact = false,
}: {
  node: StepNode;
  metadata?: any;
  onClose: () => void;
  compact?: boolean;
}) {
  const statusColors: Record<string, string> = {
    completed: "#44895f", running: "#5f84bd", pending: "#aeaeb2",
    failed: "#d65f59", waiting: "#cf9b44",
  };

  return (
    <div className="workspace-goal-node-detail" style={{
      width: compact ? "100%" : 300,
      minWidth: compact ? 0 : 300,
      flexShrink: 0,
      background: "rgba(255,255,255,0.95)", backdropFilter: "blur(12px)",
      border: "0.5px solid rgba(29, 29, 31,0.06)", borderRadius: 16,
      padding: 20, overflowY: "auto", maxHeight: compact ? 360 : 480,
    }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
        <h3 className="workspace-goal-node-detail-heading" style={{ fontSize: 14, fontWeight: 800, color: "var(--text-strong)", margin: 0 }}>
          {node.type === "goal"
            ? t("component.workspace_goal_graph.goal_details")
            : t("component.workspace_goal_graph.task_details")}
        </h3>
        <button
          onClick={onClose}
          className="workspace-goal-node-detail-close"
          style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)", fontSize: 18, lineHeight: 1 }}
        >
          x
        </button>
      </div>

      {/* Name + Status */}
      <div style={{ marginBottom: 16 }}>
        <div className="workspace-goal-node-detail-name" style={{ fontSize: 15, fontWeight: 700, color: "var(--text-strong)", marginBottom: 6 }}>{node.name}</div>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <div style={{ width: 8, height: 8, borderRadius: 4, background: statusColors[node.status] || "#aeaeb2" }} />
          <span style={{ fontSize: 12, fontWeight: 600, color: statusColors[node.status] || "#aeaeb2", textTransform: "capitalize" }}>
            {node.status}
          </span>
          {node.type && (
            <span className="workspace-goal-node-detail-type" style={{ fontSize: 10, padding: "2px 6px", borderRadius: 4, background: "var(--surface-muted)", color: "var(--text-muted)", fontWeight: 600 }}>
              {node.type}
            </span>
          )}
        </div>
      </div>

      {/* Metadata fields */}
      {metadata && (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {metadata.description && (
            <DetailField label={t("component.workspace_goal_graph.description")} value={metadata.description} />
          )}
          {metadata.metric_key && (
            <DetailField label={t("component.workspace_goal_graph.metric")} value={metadata.metric_key.replace(/_/g, " ")} />
          )}
          {metadata.current_value != null && metadata.target_value != null && (
            <DetailField label={t("component.workspace_goal_graph.progress")} value={`${metadata.current_value} / ${metadata.target_value}`} />
          )}
          {metadata.task_progress_fraction != null && (
            <DetailField
              label={t("component.workspace_goal_graph.task_progress")}
              value={`${Math.round(Number(metadata.task_progress_fraction) * 100)}%`}
            />
          )}
          {metadata.linked_task_ids && metadata.linked_task_ids.length > 0 && (
            <DetailField
              label={t("component.workspace_goal_graph.linked_tasks")}
              value={String(metadata.linked_task_ids.length)}
            />
          )}
          {metadata.estimated_impact_total != null && (
            <DetailField
              label={t("component.workspace_goal_graph.estimated_impact")}
              value={String(metadata.estimated_impact_total)}
            />
          )}
          {metadata.actual_impact_total != null && (
            <DetailField
              label={t("component.workspace_goal_graph.actual_impact")}
              value={String(metadata.actual_impact_total)}
            />
          )}
          {metadata.pace_status && (
            <DetailField
              label={t("component.workspace_goal_graph.pace")}
              value={metadata.pace_status === "tracking" ? t("page.workspace_detail.tracking") : metadata.pace_status.replace(/_/g, " ")}
            />
          )}
          {metadata.deadline && (
            <DetailField label={t("component.workspace_goal_graph.deadline")} value={metadata.deadline} />
          )}
          {metadata.kind && (
            <DetailField label={t("component.workspace_goal_graph.kind")} value={metadata.kind} />
          )}
        </div>
      )}
    </div>
  );
}

/* ── Small helpers ──────────────────────────────────────── */

function DetailField({ label, value }: { label: string; value: string }) {
  return (
    <div className="workspace-goal-node-detail-field">
      <div className="workspace-goal-node-detail-label" style={{ fontSize: 10, fontWeight: 700, color: "var(--text-muted)", }}>{label}</div>
      <div className="workspace-goal-node-detail-value" style={{ fontSize: 13, fontWeight: 600, color: "var(--text-strong)", marginTop: 2 }}>{value}</div>
    </div>
  );
}

function Legend({ color, label }: { color: string; label: string }) {
  return (
    <div className="workspace-goal-map-legend" style={{ display: "flex", alignItems: "center", gap: 4 }}>
      <div style={{ width: 10, height: 10, borderRadius: 3, background: color }} />
      <span className="workspace-goal-map-legend-label" style={{ fontSize: 11, color: "var(--text-muted)" }}>{label}</span>
    </div>
  );
}
