import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useParams, useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import type { Workspace } from "../lib/types";
import { useWorkspaceFilter } from "../stores/workspace";
import { formatDateLong as formatDate } from "../lib/format";
import PageHeader, { PageHeaderAddButton } from "../components/ui/PageHeader";
import StatusBadge from "../components/ui/StatusBadge";
import TabSwitcher from "../components/ui/TabSwitcher";
import Modal from "../components/ui/Modal";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import Button from "../components/ui/Button";
import Textarea from "../components/ui/Textarea";
import StepGraph from "../components/ui/StepGraph";
import { IconChevronRight, IconChevronLeft, IconClose, IconArrowRight } from "../components/icons";

import { t } from "../lib/i18n";
/* -- types -------------------------------------------------- */

interface GoalStep {
  id: string;
  goal_id: string;
  name: string;
  type: string;
  status: "pending" | "running" | "completed" | "failed" | "waiting";
  inputs?: Record<string, unknown>;
  outputs?: Record<string, unknown>;
  error?: string;
  duration_ms?: number;
  token_usage?: Record<string, number>;
  requires_human_input?: boolean;
  human_prompt?: string;
  order_index: number;
  created_at?: string;
}

interface Goal {
  id: string;
  entity_id: string;
  goal_text: string;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  progress: number;
  context?: Record<string, unknown>;
  result?: Record<string, unknown>;
  step_count: number;
  workspace_id?: string;
  created_at?: string;
  updated_at?: string;
}

/* -- constants ---------------------------------------------- */

const STATUS_TABS = [
  { key: "all", label: t("page.workspaces.filter_all") },
  { key: "running", label: t("page.job_logs.running") },
  { key: "completed", label: t("status.completed") },
  { key: "failed", label: t("page.dashboard.failed") },
  { key: "cancelled", label: t("status.cancelled") },
];

const STATUS_BADGE: Record<string, { type: "info" | "success" | "warning" | "danger" | "active" | "inactive"; label: string }> = {
  pending: { type: "warning", label: t("status.pending") },
  running: { type: "info", label: t("page.job_logs.running") },
  completed: { type: "success", label: t("status.completed") },
  failed: { type: "danger", label: t("page.dashboard.failed") },
  waiting: { type: "warning", label: t("page.goal_explorer.waiting") },
  cancelled: { type: "inactive", label: t("status.cancelled") },
};

const STEP_COLORS: Record<string, { border: string; bg: string; dot: string }> = {
  completed: { border: "#34d399", bg: "#f1f6f3", dot: "#44895f" },
  running: { border: "#8aa9d1", bg: "#f3f6fa", dot: "#4869ac" },
  pending: { border: "#d6d3d1", bg: "#fafaf9", dot: "#a8a29e" },
  failed: { border: "#d18b86", bg: "#f8f0ef", dot: "#c14a44" },
  waiting: { border: "#ddbb63", bg: "#faf7ef", dot: "#b27c34" },
};

/* -- helpers ------------------------------------------------ */

function formatDuration(ms?: number) {
  if (!ms) return "--";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60000).toFixed(1)}m`;
}

/* -- JSON renderer ------------------------------------------ */

function JsonView({ data, label }: { data: unknown; label: string }) {
  const [expanded, setExpanded] = useState(false);
  if (data === null || data === undefined) return null;

  return (
    <div style={{ marginTop: 8 }}>
      <button
        onClick={() => setExpanded(!expanded)}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 4,
          fontSize: 12,
          fontWeight: 600,
          color: "#78716c",
          background: "transparent",
          border: "none",
          cursor: "pointer",
          padding: 0,
        }}
      >
        <svg
          width="12"
          height="12"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
          style={{ transition: "transform 0.2s", transform: expanded ? "rotate(90deg)" : "none" }}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M8.25 4.5l7.5 7.5-7.5 7.5" />
        </svg>
        {label}
      </button>
      {expanded && (
        <pre style={{ marginTop: 4, padding: 12, background: "rgba(245,245,244,0.6)", borderRadius: 12, fontSize: 12, color: "#57534e", overflow: "auto", maxHeight: 192 }}>
          {JSON.stringify(data, null, 2)}
        </pre>
      )}
    </div>
  );
}

/* -- Create Goal Modal Content ------------------------------ */

function CreateGoalContent({ onSave }: { onSave: (text: string, workspaceId?: string) => void; saving: boolean }) {
  const [text, setText] = useState("");
  const [workspaceId, setWorkspaceId] = useState("");

  const { data: workspaces } = useQuery<Workspace[]>({
    queryKey: ["workspaces"],
    queryFn: () => api.workspaces.list(),
  });

  return (
    <form
      id="goal-form"
      onSubmit={(e) => {
        e.preventDefault();
        if (text.trim()) onSave(text, workspaceId || undefined);
      }}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <Textarea
          label={t("page.goal_explorer.goal_description")}
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={4}
          placeholder={t("page.goal_explorer.describe_the_goal_you_want_to_achieve")}
        />
        <div>
          <label style={{ display: "block", fontSize: 13, fontWeight: 600, color: "#44403c", marginBottom: 4 }}>{t("page.goal_explorer.workspace_optional")}</label>
          <select
            value={workspaceId}
            onChange={(e) => setWorkspaceId(e.target.value)}
            className="manor-input"
          >
            <option value="">{t("page.goal_explorer.default_workspace")}</option>
            {(workspaces || []).map((ws) => (
              <option key={ws.id} value={ws.id}>{ws.name}</option>
            ))}
          </select>
        </div>
      </div>
    </form>
  );
}

/* -- Step Detail Panel -------------------------------------- */

function StepDetailPanel({
  step,
  onClose,
  onHitlSubmit,
}: {
  step: GoalStep;
  onClose: () => void;
  onHitlSubmit: (input: string) => void;
}) {
  const [hitlInput, setHitlInput] = useState("");
  const badge = STATUS_BADGE[step.status] || STATUS_BADGE.pending;

  return (
    <div
      className="glass-panel"
      style={{ width: "min(100%, 350px)", flex: "1 1 280px", padding: 20, overflowY: "auto" }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
        <h3 style={{ fontSize: 14, fontWeight: 700, color: "#292524", margin: 0 }}>{t("page.goal_explorer.step_details")}</h3>
        <button
          onClick={onClose}
          style={{ color: "#a8a29e", background: "transparent", border: "none", cursor: "pointer" }}
        >
          <IconClose size={16} />
        </button>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div>
          <span style={{ fontSize: 11, color: "#a8a29e" }}>{t("page.task_collections.name")}</span>
          <p style={{ fontSize: 13, fontWeight: 600, color: "#292524", margin: "2px 0 0 0" }}>{step.name}</p>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <div>
            <span style={{ fontSize: 11, color: "#a8a29e" }}>{t("page.custom_fields.type")}</span>
            <p style={{ fontSize: 13, color: "#44403c", margin: "2px 0 0 0" }}>{step.type}</p>
          </div>
          <div>
            <span style={{ fontSize: 11, color: "#a8a29e" }}>{t("page.agent_dashboard.status")}</span>
            <div style={{ marginTop: 2 }}>
              <StatusBadge type={badge.type} dot>{badge.label}</StatusBadge>
            </div>
          </div>
        </div>

        {step.duration_ms !== undefined && (
          <div>
            <span style={{ fontSize: 11, color: "#a8a29e" }}>{t("page.agent_dashboard.duration")}</span>
            <p style={{ fontSize: 13, color: "#44403c", margin: "2px 0 0 0" }}>{formatDuration(step.duration_ms)}</p>
          </div>
        )}

        {step.token_usage && Object.keys(step.token_usage).length > 0 && (
          <div>
            <span style={{ fontSize: 11, color: "#a8a29e" }}>{t("page.goal_explorer.token_usage")}</span>
            <div style={{ display: "flex", gap: 12, marginTop: 4 }}>
              {Object.entries(step.token_usage).map(([k, v]) => (
                <span key={k} style={{ fontSize: 12, color: "#57534e" }}>
                  {k}: <strong>{v.toLocaleString()}</strong>
                </span>
              ))}
            </div>
          </div>
        )}

        <JsonView data={step.inputs} label={t("page.goal_explorer.inputs")} />
        <JsonView data={step.outputs} label={t("page.goal_explorer.outputs")} />

        {step.error && (
          <div>
            <span style={{ fontSize: 12, fontWeight: 600, color: "#c14a44" }}>{t("page.job_logs.error")}</span>
            <pre style={{ marginTop: 4, padding: 12, background: "#f8f0ef", borderRadius: 12, fontSize: 12, color: "#a23e38", overflow: "auto" }}>
              {step.error}
            </pre>
          </div>
        )}

        {step.requires_human_input && step.status === "waiting" && (
          <div style={{ borderTop: "1px solid rgba(28,25,23,0.06)", paddingTop: 12, display: "flex", flexDirection: "column", gap: 8 }}>
            <span style={{ fontSize: 12, fontWeight: 600, color: "#b27c34" }}>{t("page.goal_explorer.human_input_required")}</span>
            {step.human_prompt && (
              <p style={{ fontSize: 13, color: "#44403c" }}>{step.human_prompt}</p>
            )}
            <Textarea
              value={hitlInput}
              onChange={(e) => setHitlInput(e.target.value)}
              rows={3}
              placeholder={t("page.goal_explorer.enter_your_input")}
            />
            <Button
              variant="primary"
              onClick={() => { onHitlSubmit(hitlInput); setHitlInput(""); }}
              disabled={!hitlInput.trim()}
            >
              {t("page.goal_explorer.submit")}
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}

/* -- Goal Detail View --------------------------------------- */

function GoalDetail({ goalId }: { goalId: string }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [selectedStep, setSelectedStep] = useState<GoalStep | null>(null);

  const { data: goal, isLoading } = useQuery<Goal>({
    queryKey: ["goal", goalId],
    queryFn: () => api.goals.get(goalId),
  });

  const { data: steps = [] } = useQuery<GoalStep[]>({
    queryKey: ["goalSteps", goalId],
    queryFn: () => api.goals.getSteps(goalId),
  });

  const cancelMut = useMutation({
    mutationFn: () => api.goals.cancel(goalId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["goal", goalId] });
      queryClient.invalidateQueries({ queryKey: ["goals"] });
    },
  });

  if (isLoading || !goal) {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: "48px 0" }}>
        <LoadingSpinner size={28} />
      </div>
    );
  }

  const badge = STATUS_BADGE[goal.status] || STATUS_BADGE.pending;
  const sortedSteps = [...steps].sort((a, b) => a.order_index - b.order_index);

  return (
    <div>
      {/* Back + header */}
      <div style={{ marginBottom: 24 }}>
        <button
          onClick={() => navigate("/goals")}
          style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 13, color: "#78716c", background: "transparent", border: "none", cursor: "pointer", marginBottom: 12 }}
        >
          <IconChevronLeft size={16} />
          {t("page.goal_explorer.back_to_goals")}
        </button>
        <div className="glass-panel" style={{ padding: "24px 28px" }}>
          <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between" }}>
            <div>
              <h1 style={{ fontSize: 22, fontWeight: 800, color: "#292524", margin: 0 }}>{goal.goal_text}</h1>
              <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 8 }}>
                <StatusBadge type={badge.type} dot pulse={goal.status === "running"}>
                  {badge.label}
                </StatusBadge>
                <span style={{ fontSize: 12, color: "#a8a29e" }}>{Math.round(goal.progress)}{t("page.goal_explorer.percent_complete")}</span>
                <span style={{ fontSize: 12, color: "#a8a29e" }}>{formatDate(goal.created_at)}</span>
              </div>
            </div>
            {goal.status === "running" && (
              <Button
                variant="danger"
                onClick={() => cancelMut.mutate()}
                disabled={cancelMut.isPending}
              >
                {cancelMut.isPending ? t("page.goal_explorer.cancelling") : t("page.goal_explorer.cancel_goal")}
              </Button>
            )}
          </div>
        </div>
      </div>

      {/* Progress bar */}
      <div className="glass-card" style={{ padding: 16, marginBottom: 24 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
          <span style={{ fontSize: 12, fontWeight: 600, color: "#78716c" }}>{t("page.goal_explorer.progress")}</span>
          <span style={{ fontSize: 12, fontWeight: 700, color: "#44403c" }}>{Math.round(goal.progress)}%</span>
        </div>
        <div style={{ height: 8, background: "#f5f5f4", borderRadius: 99, overflow: "hidden" }}>
          <div
            style={{
              height: "100%",
              borderRadius: 99,
              background: "linear-gradient(90deg, #4f7d75, #436b65)",
              transition: "width 0.5s",
              width: `${goal.progress}%`,
            }}
          />
        </div>
      </div>

      {/* Step pipeline + detail panel */}
      <div style={{ display: "flex", gap: 16 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          {/* Step pipeline */}
          <div className="glass-card" style={{ padding: 20, marginBottom: 24 }}>
            <h3 style={{ fontSize: 14, fontWeight: 700, color: "#292524", margin: "0 0 16px 0" }}>
              {t("page.goal_explorer.steps")}{sortedSteps.length})
            </h3>
            <StepGraph
              steps={sortedSteps.map((s) => ({
                id: s.id,
                name: s.name,
                status: s.status,
                type: s.type,
                order_index: s.order_index,
                outputs: s.outputs as Record<string, unknown> | undefined,
              }))}
              activeStepId={selectedStep?.id}
              onStepClick={(node) => {
                const step = sortedSteps.find((s) => s.id === node.id);
                if (step) setSelectedStep(step);
              }}
            />
          </div>

          {/* Context */}
          {goal.context && Object.keys(goal.context).length > 0 && (
            <div className="glass-card" style={{ padding: 20, marginBottom: 24 }}>
              <h3 style={{ fontSize: 14, fontWeight: 700, color: "#292524", margin: "0 0 12px 0" }}>{t("page.memories.context")}</h3>
              <pre style={{ padding: 12, background: "rgba(245,245,244,0.6)", borderRadius: 12, fontSize: 12, color: "#57534e", overflow: "auto", maxHeight: 192 }}>
                {JSON.stringify(goal.context, null, 2)}
              </pre>
            </div>
          )}

          {/* Result */}
          {goal.status === "completed" && goal.result && (
            <div className="glass-card" style={{ padding: 20 }}>
              <h3 style={{ fontSize: 14, fontWeight: 700, color: "#437f6b", margin: "0 0 12px 0" }}>{t("page.goal_explorer.result")}</h3>
              <pre style={{ padding: 12, background: "#f1f6f3", borderRadius: 12, fontSize: 12, color: "#3f7361", overflow: "auto", maxHeight: 192 }}>
                {JSON.stringify(goal.result, null, 2)}
              </pre>
            </div>
          )}
        </div>

        {/* Step detail panel */}
        {selectedStep && (
          <StepDetailPanel
            step={selectedStep}
            onClose={() => setSelectedStep(null)}
            onHitlSubmit={(input) => {
              console.log("HITL input for step", selectedStep.id, ":", input);
            }}
          />
        )}
      </div>
    </div>
  );
}

/* -- Goal List View ----------------------------------------- */

function GoalList() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [statusFilter, setStatusFilter] = useState("all");
  const [createOpen, setCreateOpen] = useState(false);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const wsId = useWorkspaceFilter((s) => s.activeWorkspaceId);
  const wsFilter = wsId !== "all" ? wsId : undefined;

  const { data, isLoading } = useQuery<{ items: Goal[]; total: number }>({
    queryKey: ["goals", statusFilter, wsFilter],
    queryFn: () => {
      const params: any = { limit: 100 };
      if (statusFilter !== "all") params.status = statusFilter;
      if (wsFilter) params.workspace_id = wsFilter;
      return api.goals.list(params);
    },
  });

  const goals = data?.items ?? [];

  const createMut = useMutation({
    mutationFn: (data: { goal_text: string; workspace_id?: string }) => api.goals.create(data),
    onSuccess: (newGoal: Goal) => {
      queryClient.invalidateQueries({ queryKey: ["goals"] });
      setCreateOpen(false);
      navigate(`/goals/${newGoal.id}`);
    },
  });

  return (
    <div>
      <PageHeader
        title={t("page.goal_explorer.goal_explorer")}
        subtitle={t("page.goal_explorer.track_and_manage_autonomous_goals")}
        actions={
          <PageHeaderAddButton label={t("page.goal_explorer.add_goal")} onClick={() => setCreateOpen(true)} />
        }
      />

      <div style={{ marginBottom: 24 }}>
        <TabSwitcher tabs={STATUS_TABS} value={statusFilter} onChange={setStatusFilter} />
      </div>

      {isLoading ? (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: "48px 0" }}>
          <LoadingSpinner size={28} />
        </div>
      ) : goals.length === 0 ? (
        <EmptyState
          icon={
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#d6d3d1" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12c0 1.268-.63 2.39-1.593 3.068a3.745 3.745 0 01-1.043 3.296 3.745 3.745 0 01-3.296 1.043A3.745 3.745 0 0112 21c-1.268 0-2.39-.63-3.068-1.593a3.746 3.746 0 01-3.296-1.043 3.745 3.745 0 01-1.043-3.296A3.745 3.745 0 013 12c0-1.268.63-2.39 1.593-3.068a3.745 3.745 0 011.043-3.296 3.746 3.746 0 013.296-1.043A3.746 3.746 0 0112 3c1.268 0 2.39.63 3.068 1.593a3.746 3.746 0 013.296 1.043 3.746 3.746 0 011.043 3.296A3.745 3.745 0 0121 12z" />
            </svg>
          }
          title={statusFilter === "all" ? t("page.workspace_detail.no_goals_yet") : `No ${statusFilter} goals`}
          action={
            <button onClick={() => setCreateOpen(true)} style={{ fontSize: 13, fontWeight: 600, color: "#436b65", background: "transparent", border: "none", cursor: "pointer" }}>
              {t("page.goal_explorer.create_your_first_goal")}
            </button>
          }
        />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {goals.map((goal) => {
            const badge = STATUS_BADGE[goal.status] || STATUS_BADGE.pending;
            return (
              <div
                key={goal.id}
                onClick={() => navigate(`/goals/${goal.id}`)}
                onMouseEnter={() => setHoveredId(goal.id)}
                onMouseLeave={() => setHoveredId(null)}
                className="glass-card"
                style={{
                  cursor: "pointer",
                  borderColor: hoveredId === goal.id ? "var(--card-hover-border)" : undefined,
                  background: hoveredId === goal.id ? "var(--card-hover-bg)" : undefined,
                  boxShadow: hoveredId === goal.id ? "var(--card-hover-shadow)" : undefined,
                  transform: hoveredId === goal.id ? "var(--card-hover-transform)" : "none",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
                      <h3 style={{ fontSize: 14, fontWeight: 700, color: "#292524", margin: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{goal.goal_text}</h3>
                      <StatusBadge type={badge.type} dot pulse={goal.status === "running"}>
                        {badge.label}
                      </StatusBadge>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 16, fontSize: 12, color: "#a8a29e" }}>
                      <span>{goal.step_count} {t("page.flows.step")}{goal.step_count !== 1 ? "s" : ""}</span>
                      <span>{formatDate(goal.created_at)}</span>
                    </div>
                  </div>
                  {/* Progress bar */}
                  <div style={{ width: 128, flexShrink: 0 }}>
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 4 }}>
                      <span style={{ fontSize: 12, color: "#a8a29e" }}>{Math.round(goal.progress)}%</span>
                    </div>
                    <div style={{ height: 6, background: "#f5f5f4", borderRadius: 99, overflow: "hidden" }}>
                      <div
                        style={{
                          height: "100%",
                          borderRadius: 99,
                          transition: "width 0.5s",
                          background: goal.status === "failed" ? "#d18b86" : goal.status === "completed" ? "#44895f" : "#4f7d75",
                          width: `${goal.progress}%`,
                        }}
                      />
                    </div>
                  </div>
                  {/* Arrow */}
                  <span style={{ flexShrink: 0, color: "#d6d3d1" }}>
                    <IconChevronRight size={20} />
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      )}

      <Modal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        title={t("page.goal_explorer.create_goal")}
        footer={
          <>
            <Button variant="outline" onClick={() => setCreateOpen(false)}>{t("action.cancel")}</Button>
            <button
              type="submit"
              form="goal-form"
              disabled={createMut.isPending}
              className="btn-manor"
              style={{ opacity: createMut.isPending ? 0.5 : 1 }}
            >
              {createMut.isPending ? t("page.flows.creating") : t("action.create")}
            </button>
          </>
        }
      >
        <CreateGoalContent onSave={(text, workspaceId) => createMut.mutate({ goal_text: text, workspace_id: workspaceId })} saving={createMut.isPending} />
      </Modal>
    </div>
  );
}

/* -- Main Component ----------------------------------------- */

export default function GoalExplorer() {
  const { goalId } = useParams<{ goalId: string }>();

  if (goalId) {
    return <GoalDetail goalId={goalId} />;
  }

  return <GoalList />;
}
