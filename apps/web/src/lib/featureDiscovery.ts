/**
 * New-feature contract: register a stable id/version, place one shared hint at
 * the feature entry point, and pass a durable already-used signal. The hook
 * persists seen/dismissed/used per user. Viewing a feature retires its New
 * badge for that version; do not add a global launch modal.
 */
export const FEATURE_DISCOVERIES = {
  groupChat: { id: "group-chat", version: 1 },
  workspaceApp: { id: "workspace-app", version: 1 },
  clients: { id: "clients", version: 1 },
} as const;

export type FeatureDiscovery = { id: string; version: number };
export type FeatureDiscoveryEvent = "seen" | "dismissed" | "used";

export function featureDiscoveryPreferenceKey(
  feature: FeatureDiscovery,
  event: FeatureDiscoveryEvent,
): string {
  return `feature_discovery:${feature.id}:v${feature.version}:${event}`;
}

export function featureDiscoveryState(
  preferences: Record<string, unknown> | undefined,
  feature: FeatureDiscovery,
) {
  return {
    seen: Boolean(preferences?.[featureDiscoveryPreferenceKey(feature, "seen")]),
    dismissed: Boolean(preferences?.[featureDiscoveryPreferenceKey(feature, "dismissed")]),
    used: Boolean(preferences?.[featureDiscoveryPreferenceKey(feature, "used")]),
  };
}

export function shouldOfferFeatureDiscovery(
  preferences: Record<string, unknown> | undefined,
  feature: FeatureDiscovery,
  alreadyUsed: boolean,
): boolean {
  const state = featureDiscoveryState(preferences, feature);
  return !alreadyUsed && !state.dismissed && !state.used;
}

/** Keep an expanded hint's trigger available until the hint is closed. */
export function shouldShowFeatureDiscoveryBadge(
  preferences: Record<string, unknown> | undefined,
  feature: FeatureDiscovery,
  alreadyUsed: boolean,
  hintOpen = false,
): boolean {
  return shouldOfferFeatureDiscovery(preferences, feature, alreadyUsed)
    && (hintOpen || !featureDiscoveryState(preferences, feature).seen);
}
