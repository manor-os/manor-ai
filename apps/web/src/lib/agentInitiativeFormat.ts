import { formatDateLong, getPreferredTimeZone } from "./format";
import { t } from "./i18n";
import type {
  AgentInitiative,
  AgentInitiativeListResponse,
  AgentInitiativeRun,
  AgentInitiativeRunListResponse,
  AgentInitiativeTrigger,
} from "./agentInitiativeTypes";

export type AgentInitiativeScheduleMode = "daily" | "weekly" | "interval" | "at" | "custom";

export interface AgentInitiativeScheduleDraft {
  mode: AgentInitiativeScheduleMode;
  time: string;
  weekday: string;
  everyMinutes: string;
  runAt: string;
  timezone: string;
  original?: AgentInitiativeTrigger;
}

export function initiativeItems(response?: AgentInitiativeListResponse): AgentInitiative[] {
  if (!response) return [];
  return Array.isArray(response) ? response : response.items || [];
}

export function initiativeRunItems(response?: AgentInitiativeRunListResponse): AgentInitiativeRun[] {
  if (!response) return [];
  return Array.isArray(response) ? response : response.items || [];
}

export function initiativeTrigger(initiative: AgentInitiative): AgentInitiativeTrigger | null {
  return initiative.trigger || initiative.trigger_config || null;
}

export function initiativeCondition(initiative: AgentInitiative) {
  return initiative.condition || initiative.condition_config || null;
}

function twoDigits(value: number): string {
  return String(value).padStart(2, "0");
}

function localDateTimeValue(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  return `${date.getFullYear()}-${twoDigits(date.getMonth() + 1)}-${twoDigits(date.getDate())}T${twoDigits(date.getHours())}:${twoDigits(date.getMinutes())}`;
}

export function scheduleDraftFromTrigger(trigger?: AgentInitiativeTrigger | null): AgentInitiativeScheduleDraft {
  const fallback: AgentInitiativeScheduleDraft = {
    mode: "daily",
    time: "09:00",
    weekday: "1",
    everyMinutes: "60",
    runAt: "",
    timezone: getPreferredTimeZone(),
    original: trigger || undefined,
  };
  if (!trigger) return fallback;
  if (trigger.kind === "interval") {
    return {
      ...fallback,
      mode: "interval",
      everyMinutes: String(Math.max(1, Math.round(trigger.every_seconds / 60))),
      timezone: trigger.timezone || fallback.timezone,
    };
  }
  if (trigger.kind === "at") {
    return {
      ...fallback,
      mode: "at",
      runAt: localDateTimeValue(trigger.run_at),
      timezone: trigger.timezone || fallback.timezone,
    };
  }
  if (trigger.kind !== "cron") return { ...fallback, mode: "custom" };

  const parts = trigger.cron.trim().split(/\s+/);
  const minute = Number(parts[0]);
  const hour = Number(parts[1]);
  const clockValid = Number.isInteger(minute) && minute >= 0 && minute <= 59
    && Number.isInteger(hour) && hour >= 0 && hour <= 23;
  if (parts.length !== 5 || !clockValid || parts[2] !== "*" || parts[3] !== "*") {
    return { ...fallback, mode: "custom", timezone: trigger.timezone || fallback.timezone };
  }
  const time = `${twoDigits(hour)}:${twoDigits(minute)}`;
  if (parts[4] === "*") {
    return { ...fallback, mode: "daily", time, timezone: trigger.timezone || fallback.timezone };
  }
  if (/^[0-6]$/.test(parts[4])) {
    return {
      ...fallback,
      mode: "weekly",
      time,
      weekday: parts[4],
      timezone: trigger.timezone || fallback.timezone,
    };
  }
  return { ...fallback, mode: "custom", timezone: trigger.timezone || fallback.timezone };
}

export function triggerFromScheduleDraft(draft: AgentInitiativeScheduleDraft): AgentInitiativeTrigger {
  if (draft.mode === "custom" && draft.original) return draft.original;
  if (draft.mode === "interval") {
    const everySeconds = Math.round(Number(draft.everyMinutes) * 60);
    return {
      version: "1",
      kind: "interval",
      every_seconds: everySeconds,
      timezone: draft.timezone || getPreferredTimeZone(),
    };
  }
  if (draft.mode === "at") {
    return {
      version: "1",
      kind: "at",
      run_at: new Date(draft.runAt).toISOString(),
      timezone: draft.timezone || getPreferredTimeZone(),
    };
  }
  const [hour = "9", minute = "0"] = draft.time.split(":");
  return {
    version: "1",
    kind: "cron",
    cron: draft.mode === "weekly"
      ? `${Number(minute)} ${Number(hour)} * * ${draft.weekday}`
      : `${Number(minute)} ${Number(hour)} * * *`,
    timezone: draft.timezone || getPreferredTimeZone(),
  };
}

const WEEKDAY_KEYS = [
  "page.agent_initiative.sunday",
  "page.agent_initiative.monday",
  "page.agent_initiative.tuesday",
  "page.agent_initiative.wednesday",
  "page.agent_initiative.thursday",
  "page.agent_initiative.friday",
  "page.agent_initiative.saturday",
];

export function formatInitiativeSchedule(trigger?: AgentInitiativeTrigger | null): string {
  if (!trigger) return t("page.agent_initiative.schedule_unavailable");
  if (trigger.kind === "at") {
    return t("page.agent_initiative.once_at", { time: formatDateLong(trigger.run_at) });
  }
  if (trigger.kind === "event") {
    return t("page.agent_initiative.when_event", { source: trigger.source });
  }
  if (trigger.kind === "interval") {
    const minutes = Math.round(trigger.every_seconds / 60);
    if (minutes % 1440 === 0) {
      return t("page.agent_initiative.every_days", { count: minutes / 1440 });
    }
    if (minutes % 60 === 0) {
      return t("page.agent_initiative.every_hours", { count: minutes / 60 });
    }
    return t("page.agent_initiative.every_minutes", { count: minutes });
  }
  const parts = trigger.cron.trim().split(/\s+/);
  if (parts.length === 5 && /^\d+$/.test(parts[0]) && /^\d+$/.test(parts[1])) {
    const time = `${twoDigits(Number(parts[1]))}:${twoDigits(Number(parts[0]))}`;
    if (parts[2] === "*" && parts[3] === "*" && parts[4] === "*") {
      return t("page.agent_initiative.daily_at", { time, timezone: trigger.timezone });
    }
    if (parts[2] === "*" && parts[3] === "*" && /^[0-6]$/.test(parts[4])) {
      return t("page.agent_initiative.weekly_at", {
        day: t(WEEKDAY_KEYS[Number(parts[4])]),
        time,
        timezone: trigger.timezone,
      });
    }
  }
  return t("page.agent_initiative.custom_schedule");
}

export function initiativeStatusPresentation(status: string): { label: string; color: string; alert: boolean } {
  const presentations: Record<string, { key: string; color: string; alert?: boolean }> = {
    draft: { key: "page.agent_initiative.status_draft", color: "#8e8e93" },
    active: { key: "page.agent_initiative.status_active", color: "#437f6b" },
    muted: { key: "page.agent_initiative.status_muted", color: "#b27c34" },
    paused: { key: "page.agent_initiative.status_paused", color: "#8e8e93" },
    needs_setup: { key: "page.agent_initiative.status_needs_setup", color: "#b27c34", alert: true },
    error: { key: "page.agent_initiative.status_error", color: "#c14a44", alert: true },
    completed: { key: "page.agent_initiative.status_completed", color: "#437f6b" },
    deleted: { key: "page.agent_initiative.status_deleted", color: "#8e8e93" },
  };
  const value = presentations[status] || { key: status, color: "#8e8e93" };
  const translated = t(value.key);
  return {
    label: translated === value.key ? status.replaceAll("_", " ") : translated,
    color: value.color,
    alert: value.alert === true,
  };
}

export function decisionLabel(decision?: string | null): string {
  if (decision === "notify") return t("page.agent_initiative.decision_notify");
  if (decision === "silent") return t("page.agent_initiative.decision_silent");
  return t("page.agent_initiative.decision_pending");
}

export function decisionModeLabel(mode?: string | null): string {
  const normalized = mode === "on_match" ? "on_condition" : mode;
  if (["always", "on_condition", "on_change"].includes(normalized || "")) {
    return t(`page.agent_initiative.decision_${normalized}`);
  }
  return t("page.agent_initiative.decision_pending");
}
