export function formatBlueprintVariableValue(value) {
  if (value == null) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value) ?? "";
}

export function parseBlueprintVariableValue(input, defaultValue) {
  if (typeof defaultValue === "number") {
    const value = Number(input);
    return input.trim() && Number.isFinite(value)
      ? { ok: true, value }
      : { ok: false };
  }
  if (typeof defaultValue === "boolean") {
    const value = input.trim().toLowerCase();
    return value === "true" || value === "false"
      ? { ok: true, value: value === "true" }
      : { ok: false };
  }
  if (Array.isArray(defaultValue)) {
    try {
      const value = JSON.parse(input);
      return Array.isArray(value) ? { ok: true, value } : { ok: false };
    } catch {
      return { ok: false };
    }
  }
  if (defaultValue && typeof defaultValue === "object") {
    try {
      const value = JSON.parse(input);
      return value && typeof value === "object" && !Array.isArray(value)
        ? { ok: true, value }
        : { ok: false };
    } catch {
      return { ok: false };
    }
  }
  return { ok: true, value: input };
}
