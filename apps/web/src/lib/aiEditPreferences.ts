export enum AiEditDisplayMode {
  Live = "live",
  InstantPreview = "instant_preview",
}

export const AI_EDIT_DISPLAY_PREFERENCE_KEY = "ai_edit_display_mode";
export const DEFAULT_AI_EDIT_DISPLAY_MODE = AiEditDisplayMode.Live;
const LEGACY_AI_EDIT_PRESENTATION_PREFERENCE_KEY = "ai_edit_presentation_mode";

const DISPLAY_MODES = new Set<AiEditDisplayMode>([
  AiEditDisplayMode.Live,
  AiEditDisplayMode.InstantPreview,
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

export function isAiEditDisplayMode(value: unknown): value is AiEditDisplayMode {
  return typeof value === "string"
    && DISPLAY_MODES.has(value as AiEditDisplayMode);
}

/**
 * Preferences responses expose both flat keys and a nested `preferences`
 * object for backwards compatibility. Missing or malformed values fail open
 * to the product default: showing the edit directly in the editor.
 */
export function resolveAiEditDisplayMode(
  preferences: unknown,
): AiEditDisplayMode {
  if (!isRecord(preferences)) return DEFAULT_AI_EDIT_DISPLAY_MODE;
  const direct = preferences[AI_EDIT_DISPLAY_PREFERENCE_KEY];
  if (isAiEditDisplayMode(direct)) return direct;
  const legacyDirect = preferences[LEGACY_AI_EDIT_PRESENTATION_PREFERENCE_KEY];
  if (isAiEditDisplayMode(legacyDirect)) return legacyDirect;
  const nested = preferences.preferences;
  if (!isRecord(nested)) return DEFAULT_AI_EDIT_DISPLAY_MODE;
  const nestedValue = nested[AI_EDIT_DISPLAY_PREFERENCE_KEY];
  if (isAiEditDisplayMode(nestedValue)) return nestedValue;
  const legacyNested = nested[LEGACY_AI_EDIT_PRESENTATION_PREFERENCE_KEY];
  return isAiEditDisplayMode(legacyNested)
    ? legacyNested
    : DEFAULT_AI_EDIT_DISPLAY_MODE;
}

/** Keep the optimistic React Query value compatible with both API shapes. */
export function mergeAiEditDisplayPreference(
  current: unknown,
  mode: AiEditDisplayMode,
): Record<string, unknown> {
  const root = isRecord(current) ? current : {};
  const nested = isRecord(root.preferences) ? root.preferences : root;
  return {
    ...root,
    [AI_EDIT_DISPLAY_PREFERENCE_KEY]: mode,
    preferences: {
      ...nested,
      [AI_EDIT_DISPLAY_PREFERENCE_KEY]: mode,
    },
  };
}
