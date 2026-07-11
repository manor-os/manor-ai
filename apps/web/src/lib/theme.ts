export type ThemePreference = "white" | "dark" | "auto";
export type ResolvedTheme = "white" | "dark";

export const DEFAULT_THEME_PREFERENCE: ThemePreference = "white";
export const THEME_STORAGE_KEY = "manor-theme-preference-v2";

const THEME_VALUES = new Set<ThemePreference>(["white", "dark", "auto"]);

export function isThemePreference(value: unknown): value is ThemePreference {
  return typeof value === "string" && THEME_VALUES.has(value as ThemePreference);
}

export function preferredSystemTheme(): ResolvedTheme {
  if (typeof window === "undefined" || !window.matchMedia) return "white";
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "white";
}

export function resolveThemePreference(preference: ThemePreference): ResolvedTheme {
  return preference === "auto" ? preferredSystemTheme() : preference;
}

export function getStoredThemePreference(): ThemePreference {
  if (typeof window === "undefined") return DEFAULT_THEME_PREFERENCE;
  const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
  return isThemePreference(stored) ? stored : DEFAULT_THEME_PREFERENCE;
}

export function applyThemePreference(preference: ThemePreference): ResolvedTheme {
  const resolved = resolveThemePreference(preference);
  if (typeof document !== "undefined") {
    document.documentElement.dataset.theme = resolved;
    document.documentElement.dataset.themePreference = preference;
    document.documentElement.style.colorScheme = resolved === "dark" ? "dark" : "light";
  }
  return resolved;
}
