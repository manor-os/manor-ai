import * as marks from "./marks";
import type { BrandIcon } from "./marks";

export type IntegrationGlyph = "connection" | "email" | "webhook" | "folder" | "terminal" | "manor";
export type IntegrationBrand = {
  key: string;
  color: string;
  icon: BrandIcon | null;
  glyph?: IntegrationGlyph;
};
type BrandDefinition = Omit<IntegrationBrand, "key">;

const mark = (icon: BrandIcon, color = `#${icon.hex}`): BrandDefinition => ({ icon, color });
const functional = (glyph: IntegrationGlyph): BrandDefinition => ({ icon: null, color: "#78716c", glyph });

/** One registry for chat links, the composer, integration cards and workflow nodes.
 * This is presentation metadata, never an authentication or tool-availability gate.
 */
export const INTEGRATION_BRANDS: Readonly<Record<string, BrandDefinition>> = {
  ...Object.fromEntries(Object.entries(marks.additionalMarks).map(([key, icon]) => [key, mark(icon)])),
  gmail: mark(marks.siGmail),
  google_calendar: mark(marks.siGooglecalendar),
  google_drive: mark(marks.siGoogledrive, "#0F9D58"),
  google_sheets: mark(marks.siGooglesheets),
  google_docs: mark(marks.siGoogledocs),
  notion: mark(marks.siNotion),
  discord: mark(marks.siDiscord),
  github: mark(marks.siGithub),
  stripe: mark(marks.siStripe),
  paypal: mark(marks.siPaypal),
  robinhood: mark(marks.siRobinhood, "#000000"),
  shopify: mark(marks.siShopify),
  woocommerce: mark(marks.siWoocommerce),
  whatsapp: mark(marks.siWhatsapp),
  telegram: mark(marks.siTelegram),
  x: mark(marks.siX),
  youtube: mark(marks.siYoutube),
  tiktok: mark(marks.siTiktok),
  facebook: mark(marks.siFacebook),
  square: mark(marks.siSquare),
  wechat: mark(marks.siWechat),
  quickbooks: mark(marks.siQuickbooks),
  xiaohongshu: mark(marks.siXiaohongshu),
  email: functional("email"),
  webhook: functional("webhook"),
  ftp: functional("folder"),
  ssh: functional("terminal"),
  manor: { ...functional("manor"), color: "#436b65" },
  knowledge_local: functional("folder"),
  chrome_knowledge_local: functional("folder"),
};

/** Only visual aliases. The exact provider used in setup URLs is never rewritten. */
const ALIASES: Readonly<Record<string, string>> = {
  googlemail: "gmail", emailsend: "email", emailreadimap: "email",
  googleworkspace: "google", googlesearch: "google", googlecalendar: "google_calendar",
  googlemybusiness: "googlebusinessprofile",
  wechatpersonal: "wechat", wechatofficial: "wechat",
  twitter: "x", twitterx: "x", tiktokshop: "tiktok", rednote: "xiaohongshu",
  chrome: "googlechrome", localbrowser: "googlechrome",
  claudecode: "claude", codex: "openai", codexcli: "openai",
  geminicli: "googlegemini", gemini: "googlegemini", cursorcli: "cursor",
  continuecli: "continue", alpacamarketdata: "alpaca",
  outlook: "microsoftoutlook", onedrive: "microsoftonedrive",
  msteams: "microsoftteams", msexcel: "microsoftexcel",
  mscalendar: "microsoftoutlook", microsoftcalendar: "microsoftoutlook",
  postgres: "postgresql", raindropio: "raindrop",
  manormcpcalendar: "manor", manormcpminutes: "manor", manormcpadmin: "manor",
  manormcpfileengine: "manor",
  manorcliworker: "manor",
};

function compactKey(value: string): string {
  return value.toLowerCase().replace(/[\s_-]/g, "");
}

const KEY_INDEX = new Map(Object.keys(INTEGRATION_BRANDS).map(key => [compactKey(key), key]));

export function normalizeBrandKey(raw?: string | null): string {
  if (!raw) return "";
  let key = String(raw).trim();
  if (key.startsWith("mcp__")) key = key.split("__")[1] || "";
  else if (key.includes(".")) key = key.split(".").pop() || "";
  key = compactKey(key.replace(/(Tool|Trigger)$/i, ""));
  return Object.prototype.hasOwnProperty.call(ALIASES, key) ? ALIASES[key] : KEY_INDEX.get(key) || key;
}

export function resolveIntegrationBrand(raw?: string | null): IntegrationBrand | undefined {
  const key = normalizeBrandKey(raw);
  return Object.prototype.hasOwnProperty.call(INTEGRATION_BRANDS, key)
    ? { key, ...INTEGRATION_BRANDS[key] }
    : undefined;
}

export function getIntegrationBrandColor(raw?: string | null, fallback = "#78716c"): string {
  return resolveIntegrationBrand(raw)?.color || fallback;
}
