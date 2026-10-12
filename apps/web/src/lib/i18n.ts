import en from "./i18n/en";
import { publicTranslations } from "./i18n/public";

type TranslationCatalog = Record<string, string>;
type Locale = "en" | "zh" | "es" | "fr"
;

const translations: Partial<Record<Locale, TranslationCatalog>> & { en: TranslationCatalog } = {
  en,
};

const localeLoaders: Record<Exclude<Locale, "en">, () => Promise<{ default: TranslationCatalog }>> = {
  zh: () => import("./i18n/zh"),
  es: () => import("./i18n/es"),
  fr: () => import("./i18n/fr"),
};
const pendingLoads: Partial<Record<Locale, Promise<void>>> = {};
let localeChangeVersion = 0;

function isSupportedLocale(locale: string | null): locale is Locale {
  return locale === "en" || locale === "zh" || locale === "es" || locale === "fr"
  ;
}

export function normalizeLocale(locale: string | null | undefined): Locale {
  const base = (locale || "").trim().toLowerCase().replace("_", "-").split("-")[0];
  return isSupportedLocale(base) ? base : "en";
}

const storedLocale = localStorage.getItem("manor_locale");
let currentLocale: Locale = isSupportedLocale(storedLocale) ? storedLocale : "en";

if (storedLocale && !isSupportedLocale(storedLocale)) {
  localStorage.setItem("manor_locale", currentLocale);
}

export async function loadLocale(locale: Locale): Promise<void> {
  if (translations[locale]) return;
  if (locale === "en") return;
  if (!pendingLoads[locale]) {
    pendingLoads[locale] = localeLoaders[locale]()
      .then(({ default: catalog }) => {
        translations[locale] = catalog;
      })
      .finally(() => {
        delete pendingLoads[locale];
      });
  }
  await pendingLoads[locale];
}

export async function setLocaleIfCurrent(locale: Locale, canCommit: () => boolean): Promise<boolean> {
  const changeVersion = ++localeChangeVersion;
  await loadLocale(locale);
  if (changeVersion !== localeChangeVersion || !canCommit()) return false;
  currentLocale = locale;
  localStorage.setItem("manor_locale", locale);
  return true;
}

export async function setLocale(locale: Locale): Promise<void> {
  await setLocaleIfCurrent(locale, () => true);
}

export function getLocale(): Locale {
  return currentLocale;
}

function translate(locale: Locale, key: string, vars?: Record<string, string | number>): string {
  let str = translations[locale]?.[key]
    || (publicTranslations[locale] as Record<string, string> | undefined)?.[key]
    || translations.en[key]
    || key;
  if (vars) {
    for (const [k, v] of Object.entries(vars)) {
      // ``{name}`` placeholder syntax — existing keys without vars pass
      // through unchanged.
      str = str.replace(new RegExp(`\\{${k}\\}`, "g"), String(v));
    }
  }
  return str;
}

export function t(key: string, vars?: Record<string, string | number>): string {
  return translate(currentLocale, key, vars);
}

export function tForLocale(
  key: string,
  locale: string | null | undefined,
  vars?: Record<string, string | number>,
): string {
  return translate(normalizeLocale(locale), key, vars);
}

export async function initializeLocale(): Promise<void> {
  const initializationVersion = localeChangeVersion;
  try {
    await setLocale(currentLocale);
  } catch {
    if (localeChangeVersion !== initializationVersion + 1) return;
    currentLocale = "en";
    localStorage.setItem("manor_locale", currentLocale);
  }
}

export type { Locale };

export const SUPPORTED_LOCALES: { code: Locale; name: string; flag: string }[] = [
  { code: "en", name: "English", flag: "\uD83C\uDDFA\uD83C\uDDF8" },
  { code: "zh", name: "\u4E2D\u6587", flag: "\uD83C\uDDE8\uD83C\uDDF3" },
  { code: "es", name: "Espa\u00F1ol", flag: "\uD83C\uDDEA\uD83C\uDDF8" },
  { code: "fr", name: "Fran\u00E7ais", flag: "\uD83C\uDDEB\uD83C\uDDF7" },
];
