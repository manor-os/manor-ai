import { t } from "./i18n";

/* The server writes notification and activity text in English when the event
 * happens. These are its fixed sentences, shown in the viewer's language; any
 * other text, such as a name or an agent's message, passes through unchanged. */
const EXACT: Record<string, string> = {
  "New task": "ui.event_new_task",
  "Task assigned": "ui.event_task_assigned",
  "Task updated": "ui.event_task_updated",
  "Task retry started": "ui.event_task_retry_started",
  "Task failed": "ui.event_task_failed",
  "Task completed": "ui.event_task_completed",
  "Input needed": "ui.event_input_needed",
  "Input still needed": "ui.event_input_still_needed",
};

const PATTERNS: Array<[RegExp, string]> = [
  [/^(?<name>.+) was created\.$/s, "ui.event_was_created"],
  [/^You were assigned to (?<name>.+)\.$/s, "ui.event_assigned_to_you"],
  [/^(?<name>.+) status changed to (?<status>[\w-]+)\.$/s, "ui.event_status_changed"],
  [/^(?<name>.+) was updated\.$/s, "ui.event_was_updated"],
  [/^Retry restarted (?<name>.+)\.$/s, "ui.event_retry_restarted"],
  [/^(?<name>.+) failed and may need review\.$/s, "ui.event_failed_review"],
  [/^(?<name>.+) completed successfully\.$/s, "ui.event_completed"],
  [/^(?<name>.+) is waiting for your input\.$/s, "ui.event_waiting_input"],
  [/^(?<name>.+) is still waiting for human input\.$/s, "ui.event_still_waiting"],
  [/^New task: (?<name>.+)$/s, "ui.event_activity_new_task"],
  [/^Task '(?<name>.+)' moved to (?<status>[\w-]+)$/s, "ui.event_activity_task_moved"],
  [/^Document '(?<name>.+)' uploaded$/s, "ui.event_activity_document_uploaded"],
  [/^New agent: (?<name>.+)$/s, "ui.event_activity_new_agent"],
  [/^(?<name>.+) logged in$/s, "ui.event_activity_logged_in"],
  [/^Goal started: (?<name>.+)$/s, "ui.event_activity_goal_started"],
  [/^Goal completed: (?<name>.+)$/s, "ui.event_activity_goal_completed"],
];

const STATUS: Record<string, string> = {
  pending: "ui.event_status_pending",
  in_progress: "ui.event_status_in_progress",
  blocked: "ui.event_status_blocked",
  completed: "ui.event_status_completed",
  failed: "ui.event_status_failed",
  cancelled: "ui.event_status_cancelled",
};

export function localizeEventText(text: string): string {
  const exact = EXACT[text];
  if (exact) return t(exact);
  for (const [pattern, key] of PATTERNS) {
    const groups = pattern.exec(text)?.groups;
    if (!groups) continue;
    const status = groups.status;
    return t(key, { name: groups.name, status: status && STATUS[status] ? t(STATUS[status]) : status ?? "" });
  }
  return text;
}

/** Fixed catalog text from the server, such as a connector's one-line summary. */
export function localizeCatalogText(text: string): string {
  const key = `ui.${text.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 48)}`;
  const translated = t(key);
  return translated === key ? text : translated;
}
