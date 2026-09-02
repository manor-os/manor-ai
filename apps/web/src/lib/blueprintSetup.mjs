/** Only submit choices that belong to the current, resolved preflight. */
export function resolveBlueprintChannelSelections(requirements, selected = {}) {
  const result = {};
  for (const requirement of requirements) {
    if (requirement.kind !== "channel" || !requirement.requirement_key) continue;
    const key = requirement.requirement_key;
    const id = Object.hasOwn(selected, key) ? selected[key] : requirement.resource_id;
    if ((requirement.resource_options ?? []).some((option) => option.id === id)) {
      result[key] = id;
    }
  }
  return result;
}
