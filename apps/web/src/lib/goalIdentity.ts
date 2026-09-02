/** Stable UI identity for Goal records and portable Goal contracts. */
export function goalIdentityDedupeKey(goal: any): string {
  const id = String(goal?.id || "").trim();
  if (id) return `id:${id}`;

  const goalKey = String(goal?.goal_key || goal?.key || "").trim();
  if (goalKey) return `goal:${goalKey}`;

  // Legacy records created before goal_key existed can only fall back to
  // presentation fields. New records always take one of the stable paths.
  const metric = String(goal?.metric_key || "").trim().toLowerCase();
  if (metric) return `legacy-metric:${metric}`;
  const title = String(goal?.title || goal?.name || "").trim().toLowerCase();
  return `legacy-title:${title}`;
}
