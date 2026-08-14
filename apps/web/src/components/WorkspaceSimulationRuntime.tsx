import { useCallback, useEffect, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type WorkspaceSimulationRun } from "../lib/api";
import { getLocale } from "../lib/i18n";
import Button from "./ui/Button";
import AgentActivityOrb, { activityForRuntimeStage } from "./ui/AgentActivityOrb";
import StatusBadge from "./ui/StatusBadge";
import {
  IconBeaker,
  IconCheck,
  IconPlay,
  IconRefresh,
  IconRocket,
} from "./icons";
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
  run: WorkspaceSimulationRun | null;
  status: RuntimeStatus;
  stageIndex: number;
  stageCount: number;
  stageLabel: string;
  goalTitle: string;
  waitingMessageId: string | null;
  loading: boolean;
  restarting: boolean;
  error: string | null;
  restart: () => void;
  focusWaitingAction: () => void;
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
    refetchInterval: (result) => result.state.data?.status === "running" ? 700 : false,
    retry: 1,
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

  const focusWaitingAction = useCallback(() => {
    const id = query.data?.waiting_message_id;
    if (!id) return;
    document.getElementById(`workspace-chat-message-${id}`)?.scrollIntoView({
      behavior: "smooth",
      block: "center",
    });
  }, [query.data?.waiting_message_id]);

  const run = query.data || startMutation.data || restartMutation.data || null;
  const status = run?.status || "idle";
  const errorValue = query.error || startMutation.error || restartMutation.error;
  const error = errorValue instanceof Error ? errorValue.message : null;

  return {
    enabled,
    run,
    status,
    stageIndex: run?.stage_index || 0,
    stageCount: Math.max(1, run?.stage_count || 1),
    stageLabel: run?.stage_title || "Preparing Blueprint scenario",
    goalTitle: run?.goal_title || "Run the Blueprint scenario",
    waitingMessageId: run?.waiting_message_id || null,
    loading: query.isLoading || startMutation.isPending,
    restarting: restartMutation.isPending,
    error,
    restart: () => restartMutation.mutate(),
    focusWaitingAction,
  };
}

export function WorkspaceSimulationRuntimeBar({
  runtime,
  onPromoteToLive,
}: {
  runtime: WorkspaceSimulationRuntimeState;
  onPromoteToLive?: () => void;
}) {
  if (!runtime.enabled) return null;
  const zh = getLocale().toLowerCase().startsWith("zh");
  const isBusy = runtime.loading || runtime.status === "running";
  const statusCopy = runtime.error
    ? (zh ? "需要重试" : "Needs retry")
    : ({
        idle: zh ? "正在准备" : "Preparing",
        running: zh ? "自动执行中" : "Running automatically",
        waiting: zh ? "等待你的操作" : "Waiting for you",
        completed: zh ? "Goal 已完成" : "Goal completed",
      } satisfies Record<RuntimeStatus, string>)[runtime.status];
  const statusType = runtime.error
    ? "danger"
    : runtime.status === "completed"
      ? "success"
      : runtime.status === "waiting"
        ? "warning"
        : "teal";
  const progress = Math.round((runtime.stageIndex / runtime.stageCount) * 100);

  return (
    <section className="workspace-simulation-runtime" aria-live="polite" aria-busy={isBusy}>
      <span className="workspace-simulation-runtime__icon">
        {isBusy ? (
          <AgentActivityOrb activity={activityForRuntimeStage(runtime.stageLabel)} iconOnly />
        ) : runtime.status === "completed" ? (
          <IconCheck size={16} aria-hidden="true" />
        ) : (
          <IconBeaker size={16} aria-hidden="true" />
        )}
      </span>

      <div className="workspace-simulation-runtime__copy">
        <div className="workspace-simulation-runtime__heading">
          <strong>{runtime.run?.title || (zh ? "Workspace 模拟体验" : "Workspace simulation")}</strong>
          <StatusBadge type={statusType} dot pulse={isBusy && !runtime.error}>
            {statusCopy}
          </StatusBadge>
        </div>
        <div className="workspace-simulation-runtime__stage-row">
          <span className="workspace-simulation-runtime__count">
            {runtime.stageIndex}/{runtime.stageCount}
          </span>
          <span className="workspace-simulation-runtime__stage" title={runtime.goalTitle}>
            {runtime.error || runtime.stageLabel}
          </span>
        </div>
      </div>

      <div
        className="workspace-simulation-runtime__progress"
        role="progressbar"
        aria-label={zh ? "模拟运行进度" : "Simulation run progress"}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progress}
      >
        <span style={{ width: `${progress}%` }} />
      </div>

      <div className="workspace-simulation-runtime__actions">
        {runtime.status === "waiting" && runtime.waitingMessageId && (
          <Button size="sm" variant="primary" onClick={runtime.focusWaitingAction}>
            <IconPlay size={14} /> {zh ? "查看操作" : "View decision"}
          </Button>
        )}
        {runtime.status === "completed" && onPromoteToLive && (
          <Button
            size="sm"
            variant="primary"
            onClick={onPromoteToLive}
            title={zh ? "保留配置并转为正式运行" : "Keep this setup and promote it to live"}
          >
            <IconRocket size={14} /> {zh ? "转为正式" : "Promote to live"}
          </Button>
        )}
        {(runtime.status === "completed" || runtime.error) && (
          <Button
            size="sm"
            variant="outline"
            onClick={runtime.restart}
            loading={runtime.restarting}
          >
            <IconRefresh size={14} /> {zh ? "重新运行" : "Run again"}
          </Button>
        )}
      </div>
    </section>
  );
}
