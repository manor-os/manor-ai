import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import { useAuthStore } from "../stores/auth";
import { useConfigStore } from "../stores/config";

export type PreviewFeature =
  | "flows"
  ;

export const PREVIEW_FEATURE_FLAGS: Record<PreviewFeature, string> = {
  flows: "flows_preview_access",
};

/**
 * Combines deployment-wide availability with an account-scoped preview flag.
 * A released feature remains available to everyone; while it is Coming Soon,
 * a platform admin can opt individual accounts into the real surface.
 */
export function usePreviewFeatureAccess(feature: PreviewFeature) {
  const token = useAuthStore((state) => state.token);
  const user = useAuthStore((state) => state.user);
  const authLoading = useAuthStore((state) => state.isLoading);
  const configLoaded = useConfigStore((state) => state.loaded);
  const configured = useConfigStore((state) => {
    if (feature === "flows") return state.flows_available;
    return false;
  });
  const released = useConfigStore((state) => {
    if (feature === "flows") return state.flows_released;
    return false;
  });

  useEffect(() => {
    void useConfigStore.getState().load();
  }, []);

  const flags = useQuery({
    queryKey: ["platform-feature-flags", user?.id, user?.entity_id],
    queryFn: () => api.platform.flags(),
    enabled: Boolean(token && user),
    staleTime: 60_000,
    retry: false,
  });

  const previewEnabled = Boolean(
    flags.data?.flags?.[PREVIEW_FEATURE_FLAGS[feature]],
  );
  const accountResolutionFinished = configured
    || (!authLoading && (!token || !user || flags.isFetched));

  return {
    enabled: configured || previewEnabled,
    loaded: configLoaded && accountResolutionFinished,
    previewEnabled,
    released,
  };
}
