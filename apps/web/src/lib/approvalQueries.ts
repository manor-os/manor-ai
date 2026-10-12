import { useQuery } from "@tanstack/react-query";
import { api, ApiError } from "./api";
import { useAuthStore } from "../stores/auth";
import type { ApprovalScope } from "./approvalTypes";

export const approvalKeys = {
  entity: (entityId: string) => ["approvals", entityId] as const,
  actor: (entityId: string, actorId: string) => ["approvals", entityId, actorId] as const,
  settings: (entityId: string, actorId: string, scope: ApprovalScope) => ["approvals", entityId, actorId, scope.kind, scope.kind === "personal" ? actorId : scope.id, "settings"] as const,
  overrides: (entityId: string, actorId: string, offset: number) => ["approvals", entityId, actorId, "overrides", offset] as const,
};
export function useApprovalSettings(scope: ApprovalScope) {
  const user = useAuthStore(state => state.user);
  return useQuery({
    queryKey: approvalKeys.settings(user?.entity_id || "", user?.id || "", scope),
    queryFn: () => api.approvalSettings.get(scope),
    enabled: !!user && (scope.kind === "personal" || !!scope.id),
    staleTime: 15_000, refetchOnWindowFocus: "always",
    retry: (count, error) => !(error instanceof ApiError && [403, 404, 409, 422].includes(error.status)) && count < 1,
  });
}
