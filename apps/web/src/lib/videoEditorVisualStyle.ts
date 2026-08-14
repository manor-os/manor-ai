export const VIDEO_EDITOR_FONT_FAMILIES = ["sans", "display", "mono"] as const;
export type VideoEditorFontFamily = (typeof VIDEO_EDITOR_FONT_FAMILIES)[number];

export const VIDEO_EDITOR_TEXT_TRANSFORMS = ["none", "uppercase", "lowercase"] as const;
export type VideoEditorTextTransform = (typeof VIDEO_EDITOR_TEXT_TRANSFORMS)[number];

export const VIDEO_EDITOR_TEXT_REVEALS = ["none", "words", "characters", "wipe"] as const;
export type VideoEditorTextReveal = (typeof VIDEO_EDITOR_TEXT_REVEALS)[number];

export const VIDEO_EDITOR_FILL_TYPES = ["solid", "linear", "radial"] as const;
export type VideoEditorFillType = (typeof VIDEO_EDITOR_FILL_TYPES)[number];

export const VIDEO_EDITOR_BLEND_MODES = ["normal", "multiply", "screen", "overlay", "soft-light"] as const;
export type VideoEditorBlendMode = (typeof VIDEO_EDITOR_BLEND_MODES)[number];

export const VIDEO_EDITOR_GRAPHIC_EFFECTS = [
  "none",
  "glass",
  "glow",
  "grain",
  "scanlines",
  "chromatic",
  "vignette",
  "lightLeak",
  "filmBurn",
  "halation",
  "anamorphic",
] as const;
export type VideoEditorGraphicEffect = (typeof VIDEO_EDITOR_GRAPHIC_EFFECTS)[number];

export const VIDEO_EDITOR_MASK_SHAPES = ["none", "circle", "diamond", "hexagon"] as const;
export type VideoEditorMaskShape = (typeof VIDEO_EDITOR_MASK_SHAPES)[number];

export type CaptionVisualStyle = {
  fontFamily: VideoEditorFontFamily;
  fontWeight: number;
  letterSpacing: number;
  lineHeight: number;
  maxWidth: number;
  textTransform: VideoEditorTextTransform;
  textShadowColor: string;
  textShadowBlur: number;
  textShadowOffsetX: number;
  textShadowOffsetY: number;
  strokeColor: string;
  strokeWidth: number;
  paddingX: number;
  paddingY: number;
  cornerRadius: number;
  reveal: VideoEditorTextReveal;
  revealDuration: number;
  zIndex: number;
};

export type GraphicVisualStyle = {
  fillType: VideoEditorFillType;
  fillSecondary: string;
  gradientAngle: number;
  shadowColor: string;
  shadowBlur: number;
  shadowOffsetX: number;
  shadowOffsetY: number;
  blur: number;
  blendMode: VideoEditorBlendMode;
  effect: VideoEditorGraphicEffect;
  effectStrength: number;
  maskShape: VideoEditorMaskShape;
  assetFit: "contain" | "cover";
  zIndex: number;
  pathData: string;
};

export const DEFAULT_CAPTION_VISUAL_STYLE: Readonly<CaptionVisualStyle> = Object.freeze({
  fontFamily: "sans",
  fontWeight: 700,
  letterSpacing: 0,
  lineHeight: 1.22,
  maxWidth: 78,
  textTransform: "none",
  textShadowColor: "#000000",
  textShadowBlur: 0,
  textShadowOffsetX: 0,
  textShadowOffsetY: 0,
  strokeColor: "#000000",
  strokeWidth: 0,
  paddingX: 0.65,
  paddingY: 0.375,
  cornerRadius: 0.25,
  reveal: "none",
  revealDuration: 0.6,
  zIndex: 100,
});

export const DEFAULT_GRAPHIC_VISUAL_STYLE: Readonly<GraphicVisualStyle> = Object.freeze({
  fillType: "solid",
  fillSecondary: "#ffffff",
  gradientAngle: 0,
  shadowColor: "#000000",
  shadowBlur: 0,
  shadowOffsetX: 0,
  shadowOffsetY: 0,
  blur: 0,
  blendMode: "normal",
  effect: "none",
  effectStrength: 0.5,
  maskShape: "none",
  assetFit: "cover",
  zIndex: 0,
  pathData: "",
});

function clamp(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min;
  return Math.min(max, Math.max(min, value));
}

function finiteOr(value: unknown, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function enumValue<T extends string>(value: unknown, values: readonly T[], fallback: T): T {
  return typeof value === "string" && values.includes(value as T) ? value as T : fallback;
}

function safeColor(value: unknown, fallback: string): string {
  return typeof value === "string" && value.trim() ? value.trim().slice(0, 80) : fallback;
}

export function normalizeCaptionVisualStyle(value: Partial<CaptionVisualStyle> | undefined): CaptionVisualStyle {
  return {
    fontFamily: enumValue(value?.fontFamily, VIDEO_EDITOR_FONT_FAMILIES, DEFAULT_CAPTION_VISUAL_STYLE.fontFamily),
    fontWeight: Math.round(clamp(finiteOr(value?.fontWeight, DEFAULT_CAPTION_VISUAL_STYLE.fontWeight), 100, 900) / 100) * 100,
    letterSpacing: clamp(finiteOr(value?.letterSpacing, DEFAULT_CAPTION_VISUAL_STYLE.letterSpacing), -8, 32),
    lineHeight: clamp(finiteOr(value?.lineHeight, DEFAULT_CAPTION_VISUAL_STYLE.lineHeight), 0.8, 2.4),
    maxWidth: clamp(finiteOr(value?.maxWidth, DEFAULT_CAPTION_VISUAL_STYLE.maxWidth), 10, 96),
    textTransform: enumValue(value?.textTransform, VIDEO_EDITOR_TEXT_TRANSFORMS, DEFAULT_CAPTION_VISUAL_STYLE.textTransform),
    textShadowColor: safeColor(value?.textShadowColor, DEFAULT_CAPTION_VISUAL_STYLE.textShadowColor),
    textShadowBlur: clamp(finiteOr(value?.textShadowBlur, DEFAULT_CAPTION_VISUAL_STYLE.textShadowBlur), 0, 80),
    textShadowOffsetX: clamp(finiteOr(value?.textShadowOffsetX, DEFAULT_CAPTION_VISUAL_STYLE.textShadowOffsetX), -80, 80),
    textShadowOffsetY: clamp(finiteOr(value?.textShadowOffsetY, DEFAULT_CAPTION_VISUAL_STYLE.textShadowOffsetY), -80, 80),
    strokeColor: safeColor(value?.strokeColor, DEFAULT_CAPTION_VISUAL_STYLE.strokeColor),
    strokeWidth: clamp(finiteOr(value?.strokeWidth, DEFAULT_CAPTION_VISUAL_STYLE.strokeWidth), 0, 16),
    paddingX: clamp(finiteOr(value?.paddingX, DEFAULT_CAPTION_VISUAL_STYLE.paddingX), 0, 3),
    paddingY: clamp(finiteOr(value?.paddingY, DEFAULT_CAPTION_VISUAL_STYLE.paddingY), 0, 3),
    cornerRadius: clamp(finiteOr(value?.cornerRadius, DEFAULT_CAPTION_VISUAL_STYLE.cornerRadius), 0, 2),
    reveal: enumValue(value?.reveal, VIDEO_EDITOR_TEXT_REVEALS, DEFAULT_CAPTION_VISUAL_STYLE.reveal),
    revealDuration: clamp(finiteOr(value?.revealDuration, DEFAULT_CAPTION_VISUAL_STYLE.revealDuration), 0.05, 8),
    zIndex: Math.round(clamp(finiteOr(value?.zIndex, DEFAULT_CAPTION_VISUAL_STYLE.zIndex), -1000, 1000)),
  };
}

export function normalizeGraphicVisualStyle(value: Partial<GraphicVisualStyle> | undefined): GraphicVisualStyle {
  return {
    fillType: enumValue(value?.fillType, VIDEO_EDITOR_FILL_TYPES, DEFAULT_GRAPHIC_VISUAL_STYLE.fillType),
    fillSecondary: safeColor(value?.fillSecondary, DEFAULT_GRAPHIC_VISUAL_STYLE.fillSecondary),
    gradientAngle: clamp(finiteOr(value?.gradientAngle, DEFAULT_GRAPHIC_VISUAL_STYLE.gradientAngle), -360, 360),
    shadowColor: safeColor(value?.shadowColor, DEFAULT_GRAPHIC_VISUAL_STYLE.shadowColor),
    shadowBlur: clamp(finiteOr(value?.shadowBlur, DEFAULT_GRAPHIC_VISUAL_STYLE.shadowBlur), 0, 160),
    shadowOffsetX: clamp(finiteOr(value?.shadowOffsetX, DEFAULT_GRAPHIC_VISUAL_STYLE.shadowOffsetX), -160, 160),
    shadowOffsetY: clamp(finiteOr(value?.shadowOffsetY, DEFAULT_GRAPHIC_VISUAL_STYLE.shadowOffsetY), -160, 160),
    blur: clamp(finiteOr(value?.blur, DEFAULT_GRAPHIC_VISUAL_STYLE.blur), 0, 80),
    blendMode: enumValue(value?.blendMode, VIDEO_EDITOR_BLEND_MODES, DEFAULT_GRAPHIC_VISUAL_STYLE.blendMode),
    effect: enumValue(value?.effect, VIDEO_EDITOR_GRAPHIC_EFFECTS, DEFAULT_GRAPHIC_VISUAL_STYLE.effect),
    effectStrength: clamp(finiteOr(value?.effectStrength, DEFAULT_GRAPHIC_VISUAL_STYLE.effectStrength), 0, 1),
    maskShape: enumValue(value?.maskShape, VIDEO_EDITOR_MASK_SHAPES, DEFAULT_GRAPHIC_VISUAL_STYLE.maskShape),
    assetFit: value?.assetFit === "contain" ? "contain" : "cover",
    zIndex: Math.round(clamp(finiteOr(value?.zIndex, DEFAULT_GRAPHIC_VISUAL_STYLE.zIndex), -1000, 1000)),
    pathData: typeof value?.pathData === "string" ? value.pathData.trim().slice(0, 12000) : "",
  };
}

export function captionFontStack(family: VideoEditorFontFamily): string {
  if (family === "display") return '"Space Grotesk Variable", "Space Grotesk", "Arial Narrow", "Helvetica Neue", ui-sans-serif, system-ui, sans-serif';
  if (family === "mono") return '"JetBrains Mono", "SFMono-Regular", Consolas, monospace';
  return '"Manrope Variable", Manrope, Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
}

export function transformCaptionText(text: string, transform: VideoEditorTextTransform): string {
  if (transform === "uppercase") return text.toLocaleUpperCase();
  if (transform === "lowercase") return text.toLocaleLowerCase();
  return text;
}

export function revealedCaptionText(
  text: string,
  reveal: VideoEditorTextReveal,
  localTime: number,
  revealDuration: number,
): string {
  if (reveal === "none" || reveal === "wipe") return text;
  const progress = clamp(localTime / Math.max(0.05, revealDuration), 0, 1);
  if (reveal === "characters") {
    return text.slice(0, Math.ceil(text.length * progress));
  }
  const units = text.split(/(\s+)/);
  const words = units.filter((unit) => unit.trim()).length;
  const visibleWords = Math.ceil(words * progress);
  let seen = 0;
  return units.filter((unit) => {
    if (!unit.trim()) return seen > 0 && seen <= visibleWords;
    seen += 1;
    return seen <= visibleWords;
  }).join("").trimEnd();
}

export function graphicCssFill(
  fill: string,
  style: Pick<GraphicVisualStyle, "fillType" | "fillSecondary" | "gradientAngle">,
): string {
  if (style.fillType === "linear") {
    return `linear-gradient(${style.gradientAngle}deg, ${fill}, ${style.fillSecondary})`;
  }
  if (style.fillType === "radial") {
    return `radial-gradient(circle at 50% 42%, ${fill}, ${style.fillSecondary})`;
  }
  return fill;
}

export function graphicCssShadow(style: Pick<GraphicVisualStyle, "shadowColor" | "shadowBlur" | "shadowOffsetX" | "shadowOffsetY">): string {
  if (style.shadowBlur <= 0 && style.shadowOffsetX === 0 && style.shadowOffsetY === 0) return "none";
  return `${style.shadowOffsetX}px ${style.shadowOffsetY}px ${style.shadowBlur}px ${style.shadowColor}`;
}

export function graphicCssFilter(style: Pick<GraphicVisualStyle, "blur" | "effect" | "effectStrength" | "fillSecondary">): string {
  const filters: string[] = [];
  if (style.blur > 0) filters.push(`blur(${style.blur}px)`);
  if (style.effect === "chromatic") {
    const offset = 2 + style.effectStrength * 10;
    filters.push(`drop-shadow(${-offset}px 0 ${Math.max(0, offset * 0.2)}px rgba(255, 35, 92, 0.72))`);
    filters.push(`drop-shadow(${offset}px 0 ${Math.max(0, offset * 0.2)}px rgba(0, 220, 255, 0.68))`);
  } else if (style.effect === "glow") {
    const radius = 8 + style.effectStrength * 42;
    filters.push(`drop-shadow(0 0 ${radius}px ${style.fillSecondary})`);
  } else if (style.effect === "glass") {
    filters.push(`saturate(${1.05 + style.effectStrength * 0.45})`);
  } else if (style.effect === "halation") {
    const bloom = 12 + style.effectStrength * 38;
    filters.push(`drop-shadow(0 0 ${bloom}px rgba(255, 105, 55, ${0.24 + style.effectStrength * 0.4}))`);
    filters.push(`saturate(${1.02 + style.effectStrength * 0.35})`);
  } else if (style.effect === "lightLeak" || style.effect === "filmBurn" || style.effect === "anamorphic") {
    filters.push(`saturate(${1.04 + style.effectStrength * 0.4})`);
  }
  return filters.length > 0 ? filters.join(" ") : "none";
}

export function graphicEffectOverlayBackground(style: Pick<GraphicVisualStyle, "effect" | "effectStrength">): string {
  const strength = style.effectStrength;
  if (style.effect === "glass") {
    return `linear-gradient(125deg, rgba(255,255,255,${0.12 + strength * 0.32}), rgba(255,255,255,0.01) 46%, rgba(255,255,255,${0.04 + strength * 0.1}))`;
  }
  if (style.effect === "grain") {
    const alpha = 0.08 + strength * 0.2;
    return `repeating-radial-gradient(circle at 17% 23%, rgba(255,255,255,${alpha}) 0 0.7px, rgba(0,0,0,${alpha * 0.65}) 0.8px 1.2px, transparent 1.3px 3.2px)`;
  }
  if (style.effect === "scanlines") {
    return `repeating-linear-gradient(180deg, rgba(255,255,255,${0.02 + strength * 0.08}) 0 1px, rgba(0,0,0,${0.05 + strength * 0.14}) 1px 3px, transparent 3px 5px)`;
  }
  if (style.effect === "vignette") {
    return `radial-gradient(ellipse at center, transparent 35%, rgba(3,7,18,${0.18 + strength * 0.62}) 100%)`;
  }
  if (style.effect === "glow") {
    return `radial-gradient(circle at 50% 45%, rgba(255,255,255,${0.06 + strength * 0.18}), transparent 68%)`;
  }
  if (style.effect === "chromatic") {
    return `linear-gradient(90deg, rgba(255,35,92,${0.04 + strength * 0.12}), transparent 30% 70%, rgba(0,220,255,${0.04 + strength * 0.12}))`;
  }
  if (style.effect === "lightLeak") {
    return [
      `radial-gradient(ellipse at 9% 52%, rgba(255,238,190,${0.3 + strength * 0.5}) 0%, rgba(255,143,73,${0.18 + strength * 0.38}) 24%, rgba(255,91,43,0) 62%)`,
      `radial-gradient(ellipse at 38% 8%, rgba(255,196,118,${0.12 + strength * 0.28}) 0%, rgba(255,112,54,0) 54%)`,
      `linear-gradient(90deg, rgba(255,93,37,${0.08 + strength * 0.22}), rgba(255,183,92,0) 54%)`,
    ].join(", ");
  }
  if (style.effect === "filmBurn") {
    return [
      `radial-gradient(ellipse at -8% 54%, rgba(255,249,210,${0.5 + strength * 0.42}) 0%, rgba(255,151,63,${0.3 + strength * 0.48}) 21%, rgba(193,43,18,${0.12 + strength * 0.32}) 45%, rgba(65,8,4,0) 74%)`,
      `linear-gradient(90deg, rgba(255,67,22,${0.16 + strength * 0.28}), rgba(255,194,92,${0.05 + strength * 0.14}) 48%, rgba(0,0,0,0) 78%)`,
    ].join(", ");
  }
  if (style.effect === "halation") {
    return [
      `radial-gradient(ellipse at center, rgba(255,245,214,${0.08 + strength * 0.2}) 0%, rgba(255,112,55,${0.08 + strength * 0.2}) 34%, rgba(255,58,29,0) 70%)`,
      `linear-gradient(180deg, rgba(255,255,255,0), rgba(255,118,65,${0.04 + strength * 0.12}) 50%, rgba(255,255,255,0))`,
    ].join(", ");
  }
  if (style.effect === "anamorphic") {
    return [
      `radial-gradient(ellipse at 66% 50%, rgba(255,247,212,${0.42 + strength * 0.45}) 0%, rgba(255,170,86,${0.14 + strength * 0.26}) 10%, rgba(255,107,55,0) 34%)`,
      `linear-gradient(180deg, transparent 45%, rgba(255,176,96,${0.04 + strength * 0.12}) 48%, rgba(255,243,209,${0.22 + strength * 0.45}) 50%, rgba(255,128,61,${0.04 + strength * 0.12}) 52%, transparent 55%)`,
    ].join(", ");
  }
  return "none";
}

export function graphicCssClipPath(shape: VideoEditorMaskShape): string {
  if (shape === "circle") return "ellipse(50% 50% at 50% 50%)";
  if (shape === "diamond") return "polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)";
  if (shape === "hexagon") return "polygon(25% 3%, 75% 3%, 100% 50%, 75% 97%, 25% 97%, 0% 50%)";
  return "none";
}

export function layerZIndex(value: { zIndex?: number }, fallback = 0): number {
  return Number.isFinite(value.zIndex) ? Number(value.zIndex) : fallback;
}
