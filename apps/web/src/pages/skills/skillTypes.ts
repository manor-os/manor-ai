import { t } from "../../lib/i18n";
import { localizedFieldValue } from "../../lib/localizedContent";

export type MainTab =
  | "my"
;
export type MyScope = "entity" | "agent";

export const CATEGORIES = [
  "All",
  "Analysis",
  "Communication",
  "Data",
  "Operations",
  "brand-design",
  "launch",
  "research",
  "growth",
  "Automation",
  "Development",
];

export const THEME_COLORS: Record<
  string,
  { glow: string; gradient: string; hoverBorder: string }
> = {
  automation: {
    glow: "#34d399",
    gradient: "linear-gradient(135deg, #dceae3, #e5eeeb)",
    hoverBorder: "rgba(52,211,153,0.5)",
  },
  analysis: {
    glow: "#a78bfa",
    gradient: "linear-gradient(135deg, #ece9f5, #f5f3ff)",
    hoverBorder: "rgba(167,139,250,0.5)",
  },
  communication: {
    glow: "#38bdf8",
    gradient: "linear-gradient(135deg, #e8eff4, #f3f6fa)",
    hoverBorder: "rgba(56,189,248,0.5)",
  },
  data: {
    glow: "#ddbb63",
    gradient: "linear-gradient(135deg, #fef9c3, #f9f4ec)",
    hoverBorder: "rgba(221,187,99,0.5)",
  },
  development: {
    glow: "#fb923c",
    gradient: "linear-gradient(135deg, #ffedd5, #f8f0ef)",
    hoverBorder: "rgba(251,146,60,0.5)",
  },
  operations: {
    glow: "#f472b6",
    gradient: "linear-gradient(135deg, #f3e5ed, #fdf2f8)",
    hoverBorder: "rgba(244,114,182,0.5)",
  },
  default: {
    glow: "#a8a29e",
    gradient: "linear-gradient(135deg, #efedea, #f4f7fa)",
    hoverBorder: "rgba(28,25,23,0.2)",
  },
};

export const FALLBACK_COLORS = [
  { bg: "#efedea", fg: "#57534e" },
  { bg: "#e3e9f1", fg: "#3f57a0" },
  { bg: "#f3e5ed", fg: "#a04a72" },
  { bg: "#ece9f5", fg: "#6443a0" },
  { bg: "#f3ecd6", fg: "#936027" },
  { bg: "#dceae3", fg: "#3f7361" },
  { bg: "#e8eff4", fg: "#426c87" },
  { bg: "#f1dddb", fg: "#a23e38" },
];

export function getFallbackColor(name: string) {
  let hash = 0;
  for (let i = 0; i < name.length; i++)
    hash = name.charCodeAt(i) + ((hash << 5) - hash);
  return FALLBACK_COLORS[Math.abs(hash) % FALLBACK_COLORS.length];
}

export function getSkillTheme(category?: string) {
  if (!category) return "default";
  const t = category.toLowerCase();
  return t in THEME_COLORS ? t : "default";
}

export function formatCategory(category?: string) {
  if (!category) return "";
  const key = category.toLowerCase().replace(/[-_\s]+/g, "_");
  const labelKey = `page.skills.category_${key}`;
  const label = t(labelKey);
  if (label !== labelKey) return label;

  return category
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function getSkillDescription(skill: any): string {
  return localizedFieldValue(skill, "description", {
    translationNamespaces: ["page.skills"],
  });
}

/** Returns true if the skill has env_vars that need to be configured. */
export function skillNeedsCredentials(skill: any): boolean {
  const envVars = skill?.env_vars ?? skill?.config?.env_vars;
  return Array.isArray(envVars) && envVars.length > 0;
}

/** Returns the env_vars array from either top-level or config. */
export function getSkillEnvVars(skill: any): any[] {
  return skill?.env_vars ?? skill?.config?.env_vars ?? [];
}

export interface ParsedSkill {
  folder: string;
  name: string;
  description: string;
  prompt: string;
  tags: string[];
  is_public: boolean;
  version: string;
  valid: boolean;
  selected: boolean;
  error: string;
  id?: string;
}
