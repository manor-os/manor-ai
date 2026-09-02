import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import type { WorkspaceRecommendation } from "../lib/chatStream";
import { t } from "../lib/i18n";
import { useToastStore } from "../stores/toast";

interface WorkspaceRecommendationActionsOptions {
  enabled?: boolean;
  createWorkspace: (prompt: string) => Promise<void>;
}

export default function useWorkspaceRecommendationActions({
  enabled = true,
  createWorkspace,
}: WorkspaceRecommendationActionsOptions) {
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const navigate = useNavigate();
  const {
    data: userPreferences,
    isSuccess: workspaceRecommendationPreferencesReady,
  } = useQuery({
    queryKey: ["preferences"],
    queryFn: () => api.admin.getPreferences(),
    staleTime: 60_000,
    enabled,
  });
  const [dismissedWorkspaceRecommendations, setDismissedWorkspaceRecommendations] =
    useState<Set<string>>(() => new Set());
  const [workspaceRecommendationsSuppressedLocally, setWorkspaceRecommendationsSuppressed] =
    useState(false);
  const [workspaceRecommendationPreferenceBusy, setWorkspaceRecommendationPreferenceBusy] =
    useState(false);
  const [workspaceRecommendationBusyKey, setWorkspaceRecommendationBusyKey] =
    useState<string | null>(null);
  const workspaceRecommendationsSuppressed =
    workspaceRecommendationsSuppressedLocally ||
    userPreferences?.workspace_recommendations_enabled === false;

  const dismissWorkspaceRecommendation = useCallback((key: string) => {
    setDismissedWorkspaceRecommendations((current) => {
      const next = new Set(current);
      next.add(key);
      return next;
    });
  }, []);

  const handleWorkspaceRecommendationOptOut = useCallback(async () => {
    setWorkspaceRecommendationPreferenceBusy(true);
    try {
      const updatedPreferences = await api.admin.updatePreferences({
        workspace_recommendations_enabled: false,
      });
      queryClient.setQueryData(["preferences"], updatedPreferences);
      setWorkspaceRecommendationsSuppressed(true);
    } catch (error) {
      toast.error(
        t("component.workspace_recommendation.preference_failed"),
        error instanceof Error ? error.message : undefined,
      );
    } finally {
      setWorkspaceRecommendationPreferenceBusy(false);
    }
  }, [queryClient, toast]);

  const handleWorkspaceRecommendationCreate = useCallback(
    async (recommendation: WorkspaceRecommendation, key: string) => {
      setWorkspaceRecommendationBusyKey(key);
      try {
        await createWorkspace(
          t("component.workspace_recommendation.create_prompt", {
            request: recommendation.request,
          }),
        );
        dismissWorkspaceRecommendation(key);
      } catch (error) {
        toast.error(
          t("component.workspace_recommendation.create_failed"),
          error instanceof Error ? error.message : undefined,
        );
      } finally {
        setWorkspaceRecommendationBusyKey(null);
      }
    },
    [createWorkspace, dismissWorkspaceRecommendation, toast],
  );

  const handleWorkspaceRecommendationOpen = useCallback(
    (recommendation: WorkspaceRecommendation, key: string) => {
      if (!recommendation.workspace_id) return;
      dismissWorkspaceRecommendation(key);
      navigate(`/workspaces/${encodeURIComponent(recommendation.workspace_id)}`);
    },
    [dismissWorkspaceRecommendation, navigate],
  );

  const handleWorkspaceRecommendationAdd = useCallback(
    async (recommendation: WorkspaceRecommendation, key: string) => {
      if (!recommendation.workspace_id) return;
      setWorkspaceRecommendationBusyKey(key);
      try {
        await api.workspaces.chat.postMessage(
          recommendation.workspace_id,
          t("component.workspace_recommendation.add_prompt", {
            request: recommendation.request,
          }),
        );
        await queryClient.invalidateQueries({
          queryKey: ["workspace-chat", recommendation.workspace_id],
        });
        dismissWorkspaceRecommendation(key);
        toast.success(t("component.workspace_recommendation.added", {
          name: recommendation.workspace_name || t(
            "component.workspace_recommendation.this_workspace",
          ),
        }));
        navigate(`/workspaces/${encodeURIComponent(recommendation.workspace_id)}`);
      } catch (error) {
        toast.error(
          t("component.workspace_recommendation.add_failed"),
          error instanceof Error ? error.message : undefined,
        );
      } finally {
        setWorkspaceRecommendationBusyKey(null);
      }
    },
    [dismissWorkspaceRecommendation, navigate, queryClient, toast],
  );

  return {
    dismissedWorkspaceRecommendations,
    dismissWorkspaceRecommendation,
    handleWorkspaceRecommendationAdd,
    handleWorkspaceRecommendationCreate,
    handleWorkspaceRecommendationOpen,
    handleWorkspaceRecommendationOptOut,
    workspaceRecommendationBusyKey,
    workspaceRecommendationPreferenceBusy,
    workspaceRecommendationPreferencesReady,
    workspaceRecommendationsSuppressed,
  };
}
