// An App's own look (app_view.theme) as CSS custom properties on the App root.
// Only the accent is a colour the author chooses; every other shade is derived
// here, and the accent is darkened or lightened until text on it and around it
// stays readable, so no theme can make the page unreadable.

export type AppThemeSurface = "neutral" | "tinted" | "deep";
export type AppThemeRadius = "sharp" | "soft" | "round";
export interface AppTheme {
  accent?: string;
  surface?: AppThemeSurface;
  radius?: AppThemeRadius;
}

const RADIUS_SCALE: Record<AppThemeRadius, number> = { sharp: 0.35, soft: 1, round: 1.4 };
const LIGHT_PANEL = "#ffffff";
const DARK_PANEL = "#111111";

type Rgb = [number, number, number];

function parseHex(hex: string): Rgb | null {
  const match = /^#([0-9a-f]{6})$/i.exec(hex.trim());
  if (!match) return null;
  const value = parseInt(match[1], 16);
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}

function toHex([r, g, b]: Rgb): string {
  return `#${[r, g, b].map((part) => Math.round(Math.min(255, Math.max(0, part))).toString(16).padStart(2, "0")).join("")}`;
}

function luminance([r, g, b]: Rgb): number {
  const channel = (value: number) => {
    const c = value / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

export function contrastRatio(a: string, b: string): number {
  const [x, y] = [parseHex(a), parseHex(b)];
  if (!x || !y) return 1;
  const [hi, lo] = [luminance(x), luminance(y)].sort((p, q) => q - p);
  return (hi + 0.05) / (lo + 0.05);
}

function mix(color: Rgb, toward: Rgb, amount: number): Rgb {
  return color.map((part, index) => part + (toward[index] - part) * amount) as Rgb;
}

/** ``accent`` moved toward black (on a light panel) or white (on a dark one)
 *  until it reads at 4.5:1 against that panel. */
export function readableAccent(accent: string, dark: boolean): string {
  const start = parseHex(accent);
  if (!start) return accent;
  const panel = dark ? DARK_PANEL : LIGHT_PANEL;
  const target: Rgb = dark ? [255, 255, 255] : [0, 0, 0];
  for (let step = 0; step <= 20; step += 1) {
    const candidate = toHex(mix(start, target, step / 20));
    if (contrastRatio(candidate, panel) >= 4.5) return candidate;
  }
  return toHex(target);
}

function rgba(hex: string, alpha: number): string {
  const rgb = parseHex(hex) ?? [0, 0, 0];
  return `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${alpha})`;
}

/** Custom properties for the App root; empty for the platform's own look. */
export function appThemeStyle(theme: AppTheme | null | undefined, appearanceDark: boolean): Record<string, string> {
  if (!theme) return {};
  const dark = appearanceDark || theme.surface === "deep";
  const style: Record<string, string> = {};
  if (theme.radius) style["--app-radius-scale"] = String(RADIUS_SCALE[theme.radius]);
  if (theme.accent && parseHex(theme.accent)) {
    const accent = readableAccent(theme.accent, dark);
    const hover = toHex(mix(parseHex(accent)!, dark ? [255, 255, 255] : [0, 0, 0], 0.12));
    Object.assign(style, {
      "--accent": accent,
      "--accent-hover": hover,
      "--accent-soft": rgba(accent, dark ? 0.16 : 0.1),
      "--accent-soft-border": rgba(accent, dark ? 0.28 : 0.22),
      "--accent-ring": rgba(accent, 0.24),
    });
    if (theme.surface === "tinted") {
      style["--app-surface-tint"] = rgba(accent, dark ? 0.1 : 0.06);
    }
  }
  return style;
}
