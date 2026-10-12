import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import {
  featureDiscoveryPreferenceKey,
  featureDiscoveryState,
  shouldOfferFeatureDiscovery,
  shouldShowFeatureDiscoveryBadge,
  type FeatureDiscovery,
  type FeatureDiscoveryEvent,
} from "../lib/featureDiscovery";

/** Per-user, versioned discovery state stored in existing server preferences. */
export default function useFeatureDiscovery({
  feature,
  userId,
  enabled = true,
  active = true,
  alreadyUsed,
  viewed = false,
  autoShow,
}: {
  feature: FeatureDiscovery;
  userId?: string;
  enabled?: boolean;
  active?: boolean;
  alreadyUsed: boolean;
  /** True while the actual feature page is being viewed. */
  viewed?: boolean;
  autoShow: boolean;
}) {
  const queryClient = useQueryClient();
  const queryKey = useMemo(() => ["feature-discovery-preferences", userId] as const, [userId]);
  const { data: preferences, isSuccess } = useQuery({
    queryKey,
    queryFn: api.admin.getPreferences,
    enabled: Boolean(userId) && enabled,
    staleTime: 60_000,
  });
  const [open, setOpen] = useState(false);
  const pending = useRef(new Set<string>());
  const autoShown = useRef(new Set<string>());
  const state = featureDiscoveryState(preferences, feature);
  const offered = enabled && isSuccess && shouldOfferFeatureDiscovery(preferences, feature, alreadyUsed);
  const badgeVisible = enabled && isSuccess && !viewed
    && shouldShowFeatureDiscoveryBadge(preferences, feature, alreadyUsed, open);

  const record = useCallback((event: FeatureDiscoveryEvent) => {
    if (!userId) return;
    const key = featureDiscoveryPreferenceKey(feature, event);
    const token = `${userId}:${key}`;
    if (pending.current.has(token) || queryClient.getQueryData<Record<string, unknown>>(queryKey)?.[key]) return;
    pending.current.add(token);
    const timestamp = new Date().toISOString();
    queryClient.setQueryData<Record<string, unknown>>(queryKey, (current) => ({
      ...(current || {}),
      [key]: timestamp,
    }));
    void api.admin.updatePreferences({ [key]: timestamp }).catch(() => {
      pending.current.delete(token);
      void queryClient.invalidateQueries({ queryKey });
    });
  }, [feature, queryClient, queryKey, userId]);

  useEffect(() => {
    if (enabled && isSuccess && viewed && !state.seen) record("seen");
  }, [enabled, isSuccess, record, state.seen, viewed]);

  useEffect(() => {
    if (enabled && isSuccess && alreadyUsed && !state.used) record("used");
  }, [alreadyUsed, enabled, isSuccess, record, state.used]);

  useEffect(() => {
    if (!autoShow || !offered || state.seen || !userId) return;
    const token = `${userId}:${feature.id}:v${feature.version}`;
    if (autoShown.current.has(token)) return;
    autoShown.current.add(token);
    setOpen(true);
    record("seen");
  }, [autoShow, feature, offered, record, state.seen, userId]);

  useEffect(() => {
    if (!offered || !active) setOpen(false);
  }, [active, offered]);

  const dismiss = () => {
    if (!offered) return;
    setOpen(false);
    record("dismissed");
  };

  const reveal = () => {
    if (!offered) return;
    setOpen(true);
    record("seen");
  };

  return { offered, badgeVisible, open: offered && open, reveal, dismiss };
}
