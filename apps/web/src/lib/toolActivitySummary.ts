import { getLocale, t } from "./i18n";

const ACTIVITY_CATEGORIES = [
  { key: "read_files", displayKeys: ["file.read", "sandbox.read"], names: ["read_file", "list_files", "sandbox_read_file", "list_sandbox_files"] },
  { key: "search_files", displayKeys: ["file.search"], names: ["glob_files", "grep_files"] },
  { key: "write_files", displayKeys: ["file.write", "sandbox.write"], names: ["write_file", "sandbox_write_file", "sandbox_save_result", "save_sandbox_file"] },
  { key: "edit_files", displayKeys: ["file.edit"], names: ["edit_file"] },
  { key: "delete_files", displayKeys: ["file.delete"], names: ["delete_file"] },
  { key: "run_commands", displayKeys: ["run", "sandbox.exec", "described"], names: ["sandbox_exec"] },
  { key: "search_web", displayKeys: ["web.search"], names: ["web_search", "browse_web"] },
  { key: "read_web", displayKeys: ["web.fetch", "browser.read_page", "browser.extract"], names: ["web_fetch"] },
] as const;

/** Summarize known operations without guessing what an unknown integration did. */
export function summarizeToolActivity(activities: Array<{
  name?: string;
  display_key?: string;
  label: string;
  status?: string;
}>): string {
  const labels = activities.map((activity) => {
    if (activity.status === "unavailable") return t("component.assistant_message_blocks.website_access_denied");
    if (activity.status === "not_run") return activity.label;
    const category = ACTIVITY_CATEGORIES.find(({ displayKeys }) =>
      displayKeys.some((key) => activity.display_key === `component.assistant_process.tool.${key}`),
    ) || ACTIVITY_CATEGORIES.find(({ names }) => names.some((name) => activity.name === name));
    return category
      ? t(`component.assistant_message_blocks.activity.${category.key}`)
      : activity.label;
  });
  return new Intl.ListFormat(getLocale(), { style: "short", type: "conjunction" })
    .format([...new Set(labels.filter(Boolean))]);
}
