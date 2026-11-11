import { displayReadInterval } from "../lib/readRefreshPolicy";
import { useEffect, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type WorkspaceSimulationRun } from "../lib/api";
import { t } from "../lib/i18n";
import UserAvatar from "./ui/UserAvatar";
import { PromoteRequirementList } from "./blueprints/PromoteWorkspaceModal";
import "./WorkspaceSimulationRuntime.css";

export interface SimulationArtifact {
  id: string;
  kind: "document" | "image" | "video" | "audio" | "spreadsheet" | "presentation" | "archive";
  title: string;
  filename: string;
  mime_type: string;
  summary?: string;
  stage?: string;
  preview_url?: string;
  duration_seconds?: number;
}

type RuntimeStatus = WorkspaceSimulationRun["status"];

export interface WorkspaceSimulationRuntimeState {
  enabled: boolean;
  workspaceId: string;
  run: WorkspaceSimulationRun | null;
  status: RuntimeStatus;
  loading: boolean;
  restarting: boolean;
  error: string | null;
  restart: () => void;
}

function refreshSimulationSurface(
  queryClient: ReturnType<typeof useQueryClient>,
  workspaceId: string,
) {
  void Promise.all([
    queryClient.invalidateQueries({ queryKey: ["workspace-simulation-run", workspaceId] }),
    queryClient.invalidateQueries({ queryKey: ["workspace-chat", workspaceId] }),
    queryClient.invalidateQueries({ queryKey: ["workspaces"] }),
  ]);
}

export function useWorkspaceSimulationRuntime({
  enabled,
  workspaceId,
}: {
  enabled: boolean;
  workspaceId: string;
}): WorkspaceSimulationRuntimeState {
  const queryClient = useQueryClient();
  const autoStartAttemptRef = useRef<string | null>(null);
  const query = useQuery({
    queryKey: ["workspace-simulation-run", workspaceId],
    queryFn: () => api.workspaces.chat.simulationRun(workspaceId),
    enabled: Boolean(enabled && workspaceId),
    refetchInterval: (result) => displayReadInterval(result.state, result.state.data?.status === "running" ? 5_000 : false),
    refetchIntervalInBackground: false,
  });

  const startMutation = useMutation({
    mutationFn: () => api.workspaces.chat.startSimulationRun(workspaceId),
    onSuccess: () => refreshSimulationSurface(queryClient, workspaceId),
  });
  const restartMutation = useMutation({
    mutationFn: () => api.workspaces.chat.restartSimulationRun(workspaceId),
    onSuccess: () => refreshSimulationSurface(queryClient, workspaceId),
  });

  useEffect(() => {
    if (!enabled || !query.data || query.data.status !== "idle") return;
    if (autoStartAttemptRef.current === workspaceId || startMutation.isPending) return;
    autoStartAttemptRef.current = workspaceId;
    startMutation.mutate();
  }, [enabled, query.data, startMutation, workspaceId]);

  useEffect(() => {
    autoStartAttemptRef.current = null;
  }, [workspaceId]);

  const run = query.data || startMutation.data || restartMutation.data || null;
  const errorValue = query.error || startMutation.error || restartMutation.error;

  return {
    enabled,
    workspaceId,
    run,
    status: run?.status || "idle",
    loading: query.isLoading || startMutation.isPending,
    restarting: restartMutation.isPending,
    error: errorValue instanceof Error ? errorValue.message : null,
    restart: () => restartMutation.mutate(),
  };
}

/** The one place a simulation says where it stands: a line under the Workspace name. */
export function WorkspaceSimulationStatus({ runtime }: { runtime: WorkspaceSimulationRuntimeState }) {
  if (!runtime.enabled) return null;
  const run = runtime.run;
  const total = run?.decision_total || 0;
  const text = runtime.status === "completed"
    ? t("component.workspace_simulation.status_completed")
    : runtime.status === "waiting" && total > 0
      ? t("component.workspace_simulation.status_waiting", {
          step: Math.min(total, (run?.decision_count || 0) + 1),
          total,
        })
      : t("component.workspace_simulation.status_running");
  return <div className="workspace-simulation-status" aria-live="polite">{text}</div>;
}

export function WorkspaceSimulationChip() {
  return <span className="workspace-simulation-chip">{t("component.workspace_simulation.chip")}</span>;
}

/** Where the composer would be: why there is none, and when there will be. */
export function WorkspaceSimulationComposerNotice() {
  return (
    <div className="workspace-simulation-composer-notice" role="note">
      {t("component.workspace_simulation.composer_notice")}
    </div>
  );
}

/**
 * The end of a simulation, at the end of its chat: what happened, what live
 * running still needs, and the one way forward.
 */
export function WorkspaceSimulationCompletion({
  runtime,
  onPromote,
}: {
  runtime: WorkspaceSimulationRuntimeState;
  onPromote?: () => void;
}) {
  const completed = runtime.enabled && runtime.status === "completed";
  const preflight = useQuery({
    queryKey: ["promote-preflight", runtime.workspaceId],
    queryFn: () => api.workspaces.promotePreflight(runtime.workspaceId),
    enabled: completed && Boolean(onPromote),
  });
  if (!completed) return null;
  const decisions = runtime.run?.decision_count || 0;
  const unmet = preflight.data ?? [];
  return (
    <section className="workspace-simulation-completion" aria-label={t("component.workspace_simulation.status_completed")}>
      <UserAvatar name="Manor" type="manor" size={28} />
      <div className="workspace-simulation-completion__body">
        <p>
          {decisions > 0
            ? t("component.workspace_simulation.completed_summary", { count: decisions })
            : t("component.workspace_simulation.completed_summary_no_decisions")}
        </p>
        {unmet.length > 0 && (
          <PromoteRequirementList workspaceId={runtime.workspaceId} requirements={unmet} />
        )}
        <div className="chat-hitl-actions">
          {onPromote && (
            <button type="button" className="chat-hitl-btn-primary" onClick={onPromote}>
              {t("component.promote_workspace_modal.promote_to_live")}
            </button>
          )}
          <button
            type="button"
            className="chat-hitl-btn-secondary"
            onClick={runtime.restart}
            disabled={runtime.restarting}
          >
            {t("component.workspace_simulation.run_again")}
          </button>
        </div>
      </div>
    </section>
  );
}
