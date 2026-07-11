import en from "./i18n/en";
import zh from "./i18n/zh";
import es from "./i18n/es";

const translations = {
  en,
  zh,
  es,
} satisfies Record<string, Record<string, string>>;

type Locale = keyof typeof translations;

function isSupportedLocale(locale: string | null): locale is Locale {
  return !!locale && Object.prototype.hasOwnProperty.call(translations, locale);
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

export function setLocale(locale: Locale) {
  currentLocale = locale;
  localStorage.setItem("manor_locale", locale);
}

export function getLocale(): Locale {
  return currentLocale;
}

function translate(locale: Locale, key: string, vars?: Record<string, string | number>): string {
  let str = translations[locale]?.[key] || translations.en[key] || key;
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

export type { Locale };

export const SUPPORTED_LOCALES: { code: Locale; name: string; flag: string }[] = [
  { code: "en", name: "English", flag: "\uD83C\uDDFA\uD83C\uDDF8" },
  { code: "zh", name: "\u4E2D\u6587", flag: "\uD83C\uDDE8\uD83C\uDDF3" },
  { code: "es", name: "Espa\u00F1ol", flag: "\uD83C\uDDEA\uD83C\uDDF8" },
];
