import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import StepGraph from "./StepGraph";
import type { StepNode } from "./StepGraph";
import LoadingSpinner from "./LoadingSpinner";
import EmptyState from "./EmptyState";
import { t } from "../../lib/i18n";

/**
 * Workspace-level goal wire graph (canvas with pan/zoom).
 * Shows current goals, tasks, and execution plans as a unified DAG.
 * Goals connect to tasks, tasks connect to their plan steps, and dependent
 * tasks wait for the upstream task's leaf execution steps.
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

function goalProgressPercent(goal: any): number {
  const current = Number(goal?.current_value ?? 0);
  const target = Number(goal?.target_value ?? 1);
  const baseline = Number(goal?.baseline_value ?? 0);
  if (!Number.isFinite(current) || !Number.isFinite(target) || !Number.isFinite(baseline)) return 0;
  if (target === baseline) return current === target ? 100 : 0;
  return Math.min(100, Math.max(0, ((current - baseline) / (target - baseline)) * 100));
}

function hasMeasuredGoalValue(goal: any): boolean {
  return goal?.current_value !== null && goal?.current_value !== undefined && goal?.current_value !== "";
}

function formatGoalValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return t("page.workspace_detail.not_measured_yet");
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toLocaleString() : String(value);
}

function goalOutcomeLabel(goal: any): string {
  const target = `${t("page.workspace_detail.target")} ${formatGoalValue(goal?.target_value)}`;
  if (hasMeasuredGoalValue(goal)) {
    return `${formatGoalValue(goal.current_value)} / ${formatGoalValue(goal.target_value)}`;
  }

  const provider = String(goal?.measurement_source?.provider || "").trim().toLowerCase();
  if (provider === "manual") return `${t("component.workspace_goal_graph.manual_measurement_needed")} · ${target}`;
  if (provider === "workspace_internal") return `${t("page.workspace_detail.auto_measuring_workspace")} · ${target}`;
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

function goalGraphDedupeKey(goal: any): string {
  const metric = String(goal?.metric_key || goal?.goal_key || goal?.key || "").trim().toLowerCase();
  if (metric) return `metric:${metric}`;
  const title = String(goal?.title || goal?.name || "").trim().toLowerCase();
  if (title) return `title:${title}`;
  return `id:${String(goal?.id || "")}`;
}

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
    const key = goalGraphDedupeKey(goal);
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

function planTaskNodeId(plan: any): string {
  return plan?.task_id ? taskNodeId(String(plan.task_id)) : `plan-task-${plan.id}`;
}

const ACTIVE_PLAN_STATUSES = new Set(["running", "pending", "pending_approval", "blocked", "waiting"]);
const TERMINAL_TASK_STATUSES = new Set(["completed", "failed", "cancelled", "canceled"]);
const TERMINAL_PLAN_STATUSES = new Set(["completed", "failed", "cancelled", "canceled"]);
const HIDDEN_PLAN_STATUSES = new Set(["cancelled", "canceled"]);
const HISTORICAL_PLAN_STATUSES = new Set(["replanned"]);

function latestPlanTime(plan: any): number {
  const raw = plan?.updated_at || plan?.completed_at || plan?.created_at || "";
  const time = Date.parse(raw);
  return Number.isFinite(time) ? time : 0;
}

function relevantGraphPlans(plans: any[], goals: any[]): any[] {
  const linkedTaskIds = new Set<string>();
  for (const goal of goals) {
    for (const taskId of goal?.linked_task_ids || []) {
      if (taskId) linkedTaskIds.add(String(taskId));
    }
  }

  const byTask = new Map<string, any[]>();
  for (const plan of plans || []) {
    const status = String(plan?.status || "").toLowerCase();
    const taskStatus = String(plan?.task_status || "").toLowerCase();
    if (!plan?.id || HIDDEN_PLAN_STATUSES.has(status)) continue;
    // Ignore stale non-terminal plans for terminal tasks; those usually mean a
    // task was reconciled after a worker retry. Completed/failed terminal plans
    // still belong on the canvas as execution history.
    if (
      TERMINAL_TASK_STATUSES.has(taskStatus)
      && !TERMINAL_PLAN_STATUSES.has(status)
      && !HISTORICAL_PLAN_STATUSES.has(status)
    ) {
      continue;
    }

    const key = String(plan?.task_id || plan?.id || "");
    const bucket = byTask.get(key) || [];
    bucket.push(plan);
    byTask.set(key, bucket);
  }

  const latestPlans: any[] = [];
  for (const bucket of byTask.values()) {
    const nonHistorical = bucket.filter((plan) => !HISTORICAL_PLAN_STATUSES.has(String(plan?.status || "").toLowerCase()));
    const candidates = nonHistorical.length > 0 ? nonHistorical : bucket;
    const latest = [...candidates].sort((a, b) => latestPlanTime(b) - latestPlanTime(a))[0];
    if (latest) {
      latestPlans.push(latest);
    }
  }

  return latestPlans.sort((a, b) => {
    const aLinked = linkedTaskIds.has(String(a?.task_id || ""));
    const bLinked = linkedTaskIds.has(String(b?.task_id || ""));
    if (aLinked !== bLinked) return aLinked ? -1 : 1;
    const aActive = ACTIVE_PLAN_STATUSES.has(String(a?.status || "").toLowerCase());
    const bActive = ACTIVE_PLAN_STATUSES.has(String(b?.status || "").toLowerCase());
    if (aActive !== bActive) return aActive ? -1 : 1;
    return latestPlanTime(a) - latestPlanTime(b);
  });
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

  // Fetch active plans for workspace
  const { data: plans, isLoading: plansLoading } = useQuery({
    queryKey: ["workspace-plans", workspaceId],
    queryFn: () => api.plans.list({ workspace_id: workspaceId, limit: 100 }),
    enabled: !!workspaceId,
  });

  // Fetch tasks so the canvas can show the real hierarchy:
  // Goal -> Task -> Plan steps, plus task-to-task dependencies.
  const { data: tasksPage, isLoading: tasksLoading } = useQuery({
    queryKey: ["workspace-goal-graph-tasks", workspaceId],
    queryFn: () => api.tasks.list({ workspace_id: workspaceId, limit: 200 }),
    enabled: !!workspaceId,
  });

  const rawGoalsForPlanFilter: any[] = Array.isArray(goalsRaw) ? goalsRaw : (goalsRaw as any)?.items ?? [];
  const graphGoalsForPlanFilter = visibleGraphGoals(rawGoalsForPlanFilter);
  const graphPlansForStepFetch = relevantGraphPlans(plans || [], graphGoalsForPlanFilter);

  // Fetch steps only for plans that are useful in the goal canvas.
  const planIds: string[] = graphPlansForStepFetch.map((p: any) => p.id);
  const { data: allSteps, isLoading: stepsLoading } = useQuery({
    queryKey: ["workspace-plan-steps", workspaceId, planIds.join(",")],
    queryFn: async () => {
      const results = await Promise.all(
        planIds.map((id) => api.plans.steps(id).catch(() => []))
      );
      return results.flat();
    },
    enabled: planIds.length > 0,
  });

  const isLoading = goalsLoading || plansLoading || tasksLoading || stepsLoading;

  if (isLoading) {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: "48px 0" }}>
        <LoadingSpinner size={28} />
      </div>
    );
  }

  const rawGoalsList: any[] = Array.isArray(goalsRaw) ? goalsRaw : (goalsRaw as any)?.items ?? [];
  const goalsList = visibleGraphGoals(rawGoalsList);
  const plansList: any[] = relevantGraphPlans(plans || [], goalsList);
  const rawTasksList: any[] = Array.isArray(tasksPage) ? tasksPage : (tasksPage as any)?.items ?? [];

  if (goalsList.length === 0 && plansList.length === 0) {
    return (
      <EmptyState
        title={t("component.workspace_goal_graph.no_active_execution")}
        description={t("component.workspace_goal_graph.execution_graph_empty_desc")}
      />
    );
  }

  // Build unified graph nodes
  const graphNodes: StepNode[] = [];
  const nodeMetadata = new Map<string, any>();
  const taskGoalNodeIds = new Map<string, string[]>();
  const goalNodeIdByGoalId = new Map<string, string>();
  const taskById = new Map<string, any>();
  const latestPlanByTaskId = new Map<string, any>();

  for (const task of rawTasksList) {
    if (task?.id) taskById.set(String(task.id), task);
  }
  for (const plan of plansList) {
    if (!plan?.task_id) continue;
    const taskId = String(plan.task_id);
    latestPlanByTaskId.set(taskId, plan);
    if (!taskById.has(taskId)) {
      taskById.set(taskId, {
        id: taskId,
        title: plan.task_title || "Task",
        status: plan.task_status || plan.status || "pending",
        details: {},
      });
    }
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
  for (const plan of plansList) {
    if (plan?.task_id) graphTaskIds.add(String(plan.task_id));
  }
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

  const taskNodeByTaskId = new Map<string, StepNode>();
  const taskLeafStepNodeIds = new Map<string, string[]>();
  const orderedTaskIds = [...graphTaskIds].sort((a, b) => {
    const planA = latestPlanByTaskId.get(a);
    const planB = latestPlanByTaskId.get(b);
    return latestPlanTime(planA || taskById.get(a)) - latestPlanTime(planB || taskById.get(b));
  });

  for (const taskId of orderedTaskIds) {
    const task = taskById.get(taskId);
    const plan = latestPlanByTaskId.get(taskId);
    const goalNodeIds = taskGoalNodeIds.get(taskId) || [];
    const taskNode: StepNode = {
      id: taskNodeId(taskId),
      name: task?.title || plan?.task_title || "Task",
      status: normalizeGraphStatus(task?.status || plan?.task_status || plan?.status || "pending"),
      type: "task",
      depends_on: [...goalNodeIds],
    };
    taskNodeByTaskId.set(taskId, taskNode);
    graphNodes.push(taskNode);
    nodeMetadata.set(taskNode.id, {
      kind: "task",
      description: task?.description,
      task_id: taskId,
      task_status: task?.status || plan?.task_status,
      plan_id: plan?.id,
      plan_status: plan?.status,
      priority: task?.priority,
      dependency_task_ids: taskDependencyIds(task),
      linked_goal_ids: taskGoalIds(task),
    });
  }

  // Add plan steps
  const stepsList: any[] = allSteps || [];

  for (let i = 0; i < plansList.length; i++) {
    const plan = plansList[i];
    const planSteps = stepsList.filter((s: any) => s.plan_id === plan.id);
    const taskAnchorId = planTaskNodeId(plan);
    const planStatus = String(plan.status || "").toLowerCase();

    if (!plan.task_id) {
      graphNodes.push({
        id: taskAnchorId,
        name: plan.task_title || "Plan task",
        status: normalizeGraphStatus(plan.task_status || plan.status || "pending"),
        type: "task",
        depends_on: [],
      });
      nodeMetadata.set(taskAnchorId, {
        kind: "task",
        task_id: null,
        task_status: plan.task_status,
        plan_id: plan.id,
        plan_status: plan.status,
      });
    }

    if (planStatus === "pending_approval") {
      const nodeId = `plan-waiting-${plan.id}`;
      graphNodes.push({
        id: nodeId,
        name: plan.task_title || "Plan waiting for approval",
        status: "waiting",
        type: "task",
        depends_on: [taskAnchorId],
      });
      if (plan.task_id) taskLeafStepNodeIds.set(String(plan.task_id), [nodeId]);
      nodeMetadata.set(nodeId, {
        kind: "task",
        task_id: plan.task_id,
        task_status: plan.task_status,
        plan_id: plan.id,
        plan_status: plan.status,
      });
      continue;
    }

    if (planSteps.length === 0) {
      const dagSteps = plan.plan_dag?.steps || [];
      const dependedDagKeys = new Set<string>();
      for (const ds of dagSteps) {
        for (const dep of ds.depends_on || []) {
          if (dep) dependedDagKeys.add(String(dep));
        }
      }
      const leafNodeIds: string[] = [];
      for (let j = 0; j < dagSteps.length; j++) {
        const ds = dagSteps[j];
        const stepId = `plan-${plan.id}-${ds.key || j}`;
        const deps = (ds.depends_on || []).map((d: string) => `plan-${plan.id}-${d}`);
        if (deps.length === 0) deps.push(taskAnchorId);
        if (!dependedDagKeys.has(String(ds.key || j))) leafNodeIds.push(stepId);
        graphNodes.push({
          id: stepId,
          name: ds.key || `Step ${j + 1}`,
          status: plan.status === "completed" ? "completed" : plan.status === "running" && j === 0 ? "running" : normalizeGraphStatus(plan.status === "pending_approval" ? "waiting" : "pending"),
          type: ds.kind || "action",
          depends_on: deps,
        });
        nodeMetadata.set(stepId, {
          kind: ds.kind,
          service_key: ds.service_key,
          provider: ds.provider,
          action_key: ds.action_key,
          params: ds.params,
          plan_id: plan.id,
          plan_status: plan.status,
        });
      }
      if (plan.task_id && leafNodeIds.length > 0) taskLeafStepNodeIds.set(String(plan.task_id), leafNodeIds);
    } else {
      const dependedStepKeys = new Set<string>();
      for (const step of planSteps) {
        for (const dep of step.depends_on || []) {
          if (dep) dependedStepKeys.add(String(dep));
        }
      }
      const leafNodeIds: string[] = [];
      for (const step of planSteps) {
        const stepId = `step-${step.id}`;
        const deps = (step.depends_on || []).map((d: string) => {
          const dep = planSteps.find((s: any) => s.step_key === d);
          return dep ? `step-${dep.id}` : `plan-${plan.id}-${d}`;
        });
        if (deps.length === 0) deps.push(taskAnchorId);
        if (!dependedStepKeys.has(String(step.step_key || step.id))) leafNodeIds.push(stepId);
        graphNodes.push({
          id: stepId,
          name: step.step_key || step.kind,
          status: normalizeGraphStatus(step.step_status),
          type: step.kind || "action",
          depends_on: deps,
        });
        nodeMetadata.set(stepId, {
          kind: step.kind,
          service_key: step.service_key,
          provider: step.provider,
          action_key: step.action_key,
          params: step.params,
          result: step.result,
          error: step.error,
          cost: step.cost,
          attempt_count: step.attempt_count,
          started_at: step.started_at,
          finished_at: step.finished_at,
          plan_id: step.plan_id,
        });
      }
      if (plan.task_id && leafNodeIds.length > 0) taskLeafStepNodeIds.set(String(plan.task_id), leafNodeIds);
    }
  }

  for (const [taskId, taskNode] of taskNodeByTaskId.entries()) {
    const task = taskById.get(taskId);
    const deps = new Set(taskNode.depends_on || []);
    for (const depTaskId of taskDependencyIds(task)) {
      const upstreamLeafIds = taskLeafStepNodeIds.get(depTaskId);
      if (upstreamLeafIds?.length) {
        for (const upstreamLeafId of upstreamLeafIds) deps.add(upstreamLeafId);
      } else if (taskNodeByTaskId.has(depTaskId)) {
        deps.add(taskNodeId(depTaskId));
      }
    }
    deps.delete(taskNode.id);
    taskNode.depends_on = [...deps];
  }

  const handleNodeClick = (node: StepNode) => {
    setSelectedNode(selectedNode?.id === node.id ? null : node);
  };

  const graphStepCount = graphNodes.filter((node) => node.type !== "goal" && node.type !== "task").length;
  const linkedGoalTaskCount = taskGoalNodeIds.size;
  const graphStats = [
    { label: t("component.workspace_goal_graph.goals"), value: goalsList.length },
    { label: t("component.workspace_goal_graph.linked_tasks"), value: linkedGoalTaskCount },
    { label: t("component.workspace_goal_graph.tasks"), value: graphTaskIds.size },
    { label: t("component.workspace_goal_graph.plans"), value: plansList.length },
    { label: t("component.workspace_goal_graph.steps"), value: graphStepCount },
  ];

  // If only goals/tasks exist with no execution plans yet, keep the canvas
  // useful but explain which layer is still missing.
  if (plansList.length === 0 && goalsList.length > 0) {
    const hasTaskNodes = taskNodeByTaskId.size > 0;
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
          note={hasTaskNodes
            ? t("component.workspace_goal_graph.showing_tasks_without_plans")
            : t("component.workspace_goal_graph.waiting_for_plans")}
          height={isCompact ? 360 : 380}
        />
        <GoalStatusOverview
          goals={goalsList}
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
  note,
  height,
}: {
  graphNodes: StepNode[];
  graphStats: { label: string; value: number | string }[];
  isCompact: boolean;
  selectedNode: StepNode | null;
  selectedMetadata?: any;
  onNodeClick: (node: StepNode) => void;
  onCloseDetail: () => void;
  note?: string;
  height: number;
}) {
  return (
    <section className="workspace-goal-map-panel" style={{
      border: "1px solid rgba(28,25,23,0.06)",
      borderRadius: 18,
      background: "rgba(255,255,255,0.9)",
      overflow: "hidden",
      boxShadow: "0 16px 42px rgba(28,25,23,0.06)",
    }}>
      <div className="workspace-goal-map-header" style={{
        display: "flex",
        alignItems: "flex-start",
        justifyContent: "space-between",
        gap: 16,
        padding: "16px 18px",
        borderBottom: "1px solid rgba(28,25,23,0.06)",
        background: "linear-gradient(180deg, rgba(250,250,249,0.96), rgba(255,255,255,0.96))",
        flexWrap: "wrap",
      }}>
        <div style={{ minWidth: 220, flex: "1 1 280px" }}>
          <div className="workspace-goal-map-title" style={{ fontSize: 12, fontWeight: 850, color: "#436b65", letterSpacing: "0.08em", textTransform: "uppercase" }}>
            {t("component.workspace_goal_graph.execution_map")}
          </div>
          <div className="workspace-goal-map-description" style={{ marginTop: 4, fontSize: 12.5, lineHeight: 1.5, color: "#78716c" }}>
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
        {note && (
          <div className="workspace-goal-map-note" style={{
            marginBottom: 12,
            border: "1px solid rgba(95,146,138,0.22)",
            background: "rgba(242,246,245,0.72)",
            color: "#436b65",
            borderRadius: 12,
            padding: "10px 12px",
            fontSize: 12.5,
            lineHeight: 1.45,
          }}>
            {note}
          </div>
        )}

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
                <Legend color="#e7e5e4" label={t("component.status.pending")} />
                <Legend color="#d18b86" label={t("component.status.failed")} />
              </div>
              <span className="workspace-goal-map-hint" style={{ fontSize: 10.5, color: "#a8a29e" }}>
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
      border: "1px solid rgba(28,25,23,0.06)",
      borderRadius: 12,
      padding: "7px 10px",
      background: "#fff",
      boxShadow: "0 8px 18px rgba(28,25,23,0.04)",
    }}>
      <div className="workspace-goal-map-stat-label" style={{ fontSize: 9.5, fontWeight: 800, color: "#a8a29e", letterSpacing: "0.06em", textTransform: "uppercase" }}>
        {label}
      </div>
      <div className="workspace-goal-map-stat-value" style={{ marginTop: 2, fontSize: 15, fontWeight: 850, color: "#1c1917" }}>
        {value}
      </div>
    </div>
  );
}

function GoalStatusOverview({
  goals,
  isCompact,
  selectedGoalId,
  onSelectGoal,
}: {
  goals: any[];
  isCompact: boolean;
  selectedGoalId?: string;
  onSelectGoal: (goal: any) => void;
}) {
  if (!goals.length) return null;

  return (
    <section className="workspace-goal-overview" style={{
      border: "1px solid rgba(28,25,23,0.06)",
      borderRadius: 18,
      background: "rgba(255,255,255,0.86)",
      padding: isCompact ? 12 : 14,
      boxShadow: "0 14px 36px rgba(28,25,23,0.045)",
    }}>
      <div style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 10,
        marginBottom: 10,
      }}>
        <div className="workspace-goal-overview-title" style={{ fontSize: 11, fontWeight: 800, color: "#78716c", letterSpacing: "0.08em", textTransform: "uppercase" }}>
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
                boxShadow: selected ? "0 12px 30px rgba(67,107,101,0.14)" : "0 8px 20px rgba(28,25,23,0.04)",
                cursor: "pointer",
              }}
            >
              <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 10, marginBottom: 9 }}>
                <div className="workspace-goal-overview-card-title" style={{
                  minWidth: 0,
                  fontSize: 12,
                  fontWeight: 800,
                  color: "#1c1917",
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
                  textTransform: "uppercase",
                }}>
                  {goalStatusLabel(goal)}
                </span>
              </div>

              <div style={{ display: "flex", justifyContent: "space-between", gap: 10, marginBottom: 4 }}>
                <span className="workspace-goal-overview-label" style={{ fontSize: 10, fontWeight: 800, color: "#57534e", textTransform: "uppercase", letterSpacing: "0.04em" }}>
                  {t("component.workspace_goal_graph.progress")}
                </span>
                <span className="workspace-goal-overview-percent" style={{ fontSize: 10.5, fontWeight: 800, color: measured ? tone.fg : "#a8a29e" }}>
                  {completionPercent}%
                </span>
              </div>
              <div className="workspace-goal-overview-track" style={{ height: 5, borderRadius: 999, background: "#e7e5e4", overflow: "hidden", marginBottom: 9 }}>
                <div style={{
                  width: `${outcomeProgress}%`,
                  height: "100%",
                  borderRadius: 999,
                  background: measured ? tone.bar : "#d6d3d1",
                  transition: "width 0.45s ease",
                }} />
              </div>
              <div className="workspace-goal-overview-outcome" style={{ fontSize: 10.5, color: "#78716c", marginBottom: 9 }}>
                {goalOutcomeLabel(goal)}
              </div>

              <div style={{ display: "flex", justifyContent: "space-between", gap: 10, marginBottom: 4 }}>
                <span className="workspace-goal-overview-label" style={{ fontSize: 10, fontWeight: 800, color: "#57534e", textTransform: "uppercase", letterSpacing: "0.04em" }}>
                  {t("component.workspace_goal_graph.execution")}
                </span>
                <span className="workspace-goal-overview-execution" style={{ fontSize: 10.5, fontWeight: 700, color: linkedTaskCount > 0 ? "#436b65" : "#a8a29e" }}>
                  {executionLabel}
                </span>
              </div>

              {taskStatusParts.length > 0 && (
                <div style={{ marginTop: 7, display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
                  <span className="workspace-goal-overview-status-parts" style={{ fontSize: 10, color: "#a8a29e" }}>
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
    completed: "#44895f", running: "#5f84bd", pending: "#a8a29e",
    failed: "#d65f59", waiting: "#cf9b44",
  };

  return (
    <div className="workspace-goal-node-detail" style={{
      width: compact ? "100%" : 300,
      minWidth: compact ? 0 : 300,
      flexShrink: 0,
      background: "rgba(255,255,255,0.95)", backdropFilter: "blur(12px)",
      border: "1px solid rgba(28,25,23,0.06)", borderRadius: 16,
      padding: 20, overflowY: "auto", maxHeight: compact ? 360 : 480,
    }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
        <h3 className="workspace-goal-node-detail-heading" style={{ fontSize: 14, fontWeight: 800, color: "#1c1917", margin: 0 }}>
          {node.type === "goal"
            ? t("component.workspace_goal_graph.goal_details")
            : t("component.workspace_goal_graph.step_details")}
        </h3>
        <button
          onClick={onClose}
          className="workspace-goal-node-detail-close"
          style={{ background: "none", border: "none", cursor: "pointer", color: "#a8a29e", fontSize: 18, lineHeight: 1 }}
        >
          x
        </button>
      </div>

      {/* Name + Status */}
      <div style={{ marginBottom: 16 }}>
        <div className="workspace-goal-node-detail-name" style={{ fontSize: 15, fontWeight: 700, color: "#292524", marginBottom: 6 }}>{node.name}</div>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <div style={{ width: 8, height: 8, borderRadius: 4, background: statusColors[node.status] || "#a8a29e" }} />
          <span style={{ fontSize: 12, fontWeight: 600, color: statusColors[node.status] || "#a8a29e", textTransform: "capitalize" }}>
            {node.status}
          </span>
          {node.type && (
            <span className="workspace-goal-node-detail-type" style={{ fontSize: 10, padding: "2px 6px", borderRadius: 4, background: "#f5f5f4", color: "#78716c", fontWeight: 600 }}>
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
          {metadata.service_key && (
            <DetailField label={t("component.workspace_goal_graph.service")} value={metadata.service_key} />
          )}
          {metadata.provider && (
            <DetailField label={t("component.workspace_goal_graph.provider")} value={metadata.provider} />
          )}
          {metadata.action_key && (
            <DetailField label={t("component.workspace_goal_graph.action")} value={metadata.action_key} />
          )}
          {metadata.started_at && (
            <DetailField label={t("component.workspace_goal_graph.started")} value={new Date(metadata.started_at).toLocaleString()} />
          )}
          {metadata.finished_at && (
            <DetailField label={t("component.workspace_goal_graph.finished")} value={new Date(metadata.finished_at).toLocaleString()} />
          )}
          {metadata.attempt_count != null && (
            <DetailField label={t("component.workspace_goal_graph.attempts")} value={String(metadata.attempt_count)} />
          )}
          {metadata.cost && Object.keys(metadata.cost).length > 0 && (
            <DetailField label={t("component.workspace_goal_graph.cost")} value={JSON.stringify(metadata.cost)} mono />
          )}
          {metadata.error && (
            <div style={{ marginTop: 4 }}>
              <div className="workspace-goal-node-detail-error-label" style={{ fontSize: 10, fontWeight: 700, color: "#c14a44", marginBottom: 4 }}>{t("component.workspace_goal_graph.error")}</div>
              <pre className="workspace-goal-node-detail-pre workspace-goal-node-detail-pre--error" style={{
                fontSize: 11, padding: 10, borderRadius: 8,
                background: "#f8f0ef", color: "#a23e38",
                overflow: "auto", maxHeight: 100, margin: 0,
                whiteSpace: "pre-wrap", wordBreak: "break-word",
              }}>
                {typeof metadata.error === "string" ? metadata.error : JSON.stringify(metadata.error, null, 2)}
              </pre>
            </div>
          )}
          {metadata.result && (
            <div style={{ marginTop: 4 }}>
              <div className="workspace-goal-node-detail-result-label" style={{ fontSize: 10, fontWeight: 700, color: "#44895f", marginBottom: 4 }}>{t("component.workspace_goal_graph.result")}</div>
              <pre className="workspace-goal-node-detail-pre workspace-goal-node-detail-pre--result" style={{
                fontSize: 11, padding: 10, borderRadius: 8,
                background: "#f3f8f4", color: "#3a6047",
                overflow: "auto", maxHeight: 120, margin: 0,
                whiteSpace: "pre-wrap", wordBreak: "break-word",
              }}>
                {JSON.stringify(metadata.result, null, 2)}
              </pre>
            </div>
          )}
          {metadata.params && Object.keys(metadata.params).length > 0 && (
            <div style={{ marginTop: 4 }}>
              <div className="workspace-goal-node-detail-params-label" style={{ fontSize: 10, fontWeight: 700, color: "#78716c", marginBottom: 4 }}>{t("component.workspace_goal_graph.params")}</div>
              <pre className="workspace-goal-node-detail-pre workspace-goal-node-detail-pre--params" style={{
                fontSize: 11, padding: 10, borderRadius: 8,
                background: "#fafaf9", color: "#57534e",
                overflow: "auto", maxHeight: 120, margin: 0,
                whiteSpace: "pre-wrap", wordBreak: "break-word",
              }}>
                {JSON.stringify(metadata.params, null, 2)}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ── Small helpers ──────────────────────────────────────── */

function DetailField({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="workspace-goal-node-detail-field">
      <div className="workspace-goal-node-detail-label" style={{ fontSize: 10, fontWeight: 700, color: "#a8a29e", textTransform: "uppercase", letterSpacing: "0.05em" }}>{label}</div>
      <div className="workspace-goal-node-detail-value" style={{ fontSize: 13, fontWeight: 600, color: "#292524", marginTop: 2, fontFamily: mono ? "monospace" : "inherit" }}>{value}</div>
    </div>
  );
}

function Legend({ color, label }: { color: string; label: string }) {
  return (
    <div className="workspace-goal-map-legend" style={{ display: "flex", alignItems: "center", gap: 4 }}>
      <div style={{ width: 10, height: 10, borderRadius: 3, background: color }} />
      <span className="workspace-goal-map-legend-label" style={{ fontSize: 11, color: "#78716c" }}>{label}</span>
    </div>
  );
}
