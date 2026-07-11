import { getLocale, t } from "./i18n";

function normalizeTranslationKey(value: unknown): string {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/[_\s]+/g, "-")
    .replace(/[^a-z0-9-]+/g, "")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
}

function localizedValueFromMap(value: unknown): string {
  if (!value || typeof value !== "object") return "";
  const map = value as Record<string, unknown>;
  const locale = getLocale();
  const direct = map[locale];
  const fallback = map[locale.split("-")[0]] || map.en;
  const resolved = direct || fallback;
  return typeof resolved === "string" ? resolved.trim() : "";
}

function translatedFieldValue(
  item: any,
  field: string,
  translationNamespaces: string[],
): string {
  const candidates = [
    item?.marketplace_id,
    item?.config?.marketplace_id,
    item?.slug,
    item?.id,
    item?.name,
    item?.display_name,
    item?.displayName,
    item?.skill_name,
    item?.config?.skill_name,
  ];

  for (const namespace of translationNamespaces) {
    for (const candidate of candidates) {
      const keyPart = normalizeTranslationKey(candidate);
      if (!keyPart) continue;
      const translationKey = `${namespace}.${field}.${keyPart}`;
      const translated = t(translationKey);
      if (translated !== translationKey) return translated;
    }
  }
  return "";
}

export function localizedFieldValue(
  item: any,
  field: string,
  options: { translationNamespaces?: string[] } = {},
): string {
  const locale = getLocale();
  const localizedKey = `localized_${field}`;
  const i18nKey = `${field}_i18n`;
  const translationNamespaces = options.translationNamespaces || [];
  const candidates = [
    item?.[localizedKey],
    localizedValueFromMap(item?.[i18nKey]),
    localizedValueFromMap(item?.config?.[i18nKey]),
    typeof item?.i18n?.[locale]?.[field] === "string"
      ? item.i18n[locale][field]
      : "",
    typeof item?.config?.i18n?.[locale]?.[field] === "string"
      ? item.config.i18n[locale][field]
      : "",
    translatedFieldValue(item, field, translationNamespaces),
    item?.[field],
    item?.config?.[field],
  ];

  for (const candidate of candidates) {
    if (typeof candidate === "string" && candidate.trim()) return candidate.trim();
  }
  return "";
}

export function getAgentDescription(agent: any): string {
  return localizedFieldValue(agent, "description", {
    translationNamespaces: ["page.agents"],
  });
}
