/**
 * Resolve a workflow connector node to its real brand logo + colour, so the
 * canvas shows the Gmail / GitHub / Notion mark (n8n-style) instead of one
 * generic glyph. Reuses the vendored simple-icons marks in ``brandIcons.ts``;
 * brands without a vendored logo still get their brand colour (the node falls
 * back to a tinted generic glyph).
 */
import {
  siNotion, siGmail, siDiscord, siGithub, siStripe, siPaypal, siShopify,
  siWoocommerce, siGooglecalendar, siGoogledrive, siWhatsapp, siTelegram,
  siX, siYoutube, siTiktok, siFacebook, siSquare, siWechat, siQuickbooks,
  siXiaohongshu, siGooglesheets, siGoogledocs, type BrandIcon,
} from "./brandIcons";

// normalized key -> vendored logo
const BRAND_ICON: Record<string, BrandIcon> = {
  gmail: siGmail, email: siGmail, emailsend: siGmail, emailreadimap: siGmail,
  notion: siNotion, discord: siDiscord, github: siGithub, stripe: siStripe,
  paypal: siPaypal, shopify: siShopify, woocommerce: siWoocommerce,
  googlecalendar: siGooglecalendar, googledrive: siGoogledrive,
  googlesheets: siGooglesheets, googledocs: siGoogledocs, whatsapp: siWhatsapp,
  telegram: siTelegram, x: siX, twitter: siX, youtube: siYoutube,
  tiktok: siTiktok, facebook: siFacebook, square: siSquare, wechat: siWechat,
  quickbooks: siQuickbooks, xiaohongshu: siXiaohongshu,
};

// brand colour for marks we don't vendor a logo for (tints the generic glyph)
const BRAND_COLOR: Record<string, string> = {
  slack: "#4A154B", twilio: "#F22F46", sendgrid: "#1A82E2", linkedin: "#0A66C2",
  hubspot: "#FF7A59", airtable: "#18BFFF", jira: "#0052CC", linear: "#5E6AD2",
  salesforce: "#00A1E0", zendesk: "#03363D", trello: "#0052CC", asana: "#F06A6A",
  mailchimp: "#FFE01B", dropbox: "#0061FF", intercom: "#1F8DED", gitlab: "#FC6D26",
  openai: "#10A37F", openrouter: "#10A37F", anthropic: "#D97757", brightdata: "#0F4C81",
  mindee: "#5A4FCF", clickup: "#7B68EE", pipedrive: "#1A1A1A", freshdesk: "#25C16F",
  todoist: "#E44332", raindrop: "#0CA4EB", mailerlite: "#09C269", googletasks: "#4285F4",
  postgres: "#4169E1", mysql: "#4479A1", mongodb: "#47A248", redis: "#FF4438",
  supabase: "#3FCF8E", elasticsearch: "#005571", agilecrm: "#0A9DE2", storyblok: "#0BB07F",
  baserow: "#4C6FFF", clockify: "#03A9F4", ftp: "#6f6861", ssh: "#6f6861",
};

const ALIAS: Record<string, string> = {
  googlemail: "gmail", twitterx: "x", microsoftteams: "msteams", microsoftoutlook: "outlook",
};

/** Strip an MCP/n8n type down to a bare, normalized brand key. */
export function normalizeBrandKey(raw?: string | null): string {
  if (!raw) return "";
  let k = String(raw).trim();
  if (k.startsWith("mcp__")) k = k.split("__")[1] || "";
  else if (k.includes(".")) k = k.split(".").pop() || ""; // n8n-nodes-base.gmailTool -> gmailTool
  k = k.replace(/(Tool|Trigger)$/i, "");                  // gmailTool -> gmail
  k = k.toLowerCase().replace(/[\s_-]/g, "");             // google_drive -> googledrive
  return ALIAS[k] || k;
}

/** Brand for a connector-ish step. ``icon`` is the real logo when we have one;
 *  ``color`` is the brand colour (empty → caller uses the node's category hue). */
export function resolveConnectorBrand(raw?: string | null): { color: string; icon: BrandIcon | null } {
  const key = normalizeBrandKey(raw);
  const icon = BRAND_ICON[key] || null;
  const color = BRAND_COLOR[key] || (icon ? `#${icon.hex}` : "");
  return { color, icon };
}
