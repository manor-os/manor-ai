import type { MotionEasing, MotionKeyframe, MotionPathMode, MotionPose } from "./videoEditorMotion";
import {
  DEFAULT_CAPTION_VISUAL_STYLE,
  DEFAULT_GRAPHIC_VISUAL_STYLE,
} from "./videoEditorVisualStyle";
import type {
  CaptionVisualStyle,
  GraphicVisualStyle,
  VideoEditorFontFamily,
} from "./videoEditorVisualStyle";
import type { ParticleMotion, ParticleShape } from "./videoEditorParticles";
import type { VideoEditorShaderStyle } from "./videoEditorShader";

export const MOTION_DESIGN_PRESETS = [
  "kinetic-type",
  "swiss-grid",
  "product-promo",
  "editorial-data",
  "product-film",
  "feature-reveal",
  "product-story",
  "motion-design",
  "texture-launch",
] as const;

export type MotionDesignPreset = (typeof MOTION_DESIGN_PRESETS)[number];

export type MotionDesignRequest = {
  preset?: MotionDesignPreset;
  mode?: "replace" | "append";
  headline?: string;
  kicker?: string;
  subhead?: string;
  cta?: string;
  statValue?: string;
  statLabel?: string;
  background?: string;
  surface?: string;
  foreground?: string;
  accent?: string;
  accent2?: string;
  quality?: "draft" | "production";
  visualTone?: "editorial" | "cinematic" | "technical" | "playful";
  fontFamily?: VideoEditorFontFamily;
};

export type GeneratedMotionCaption = CaptionVisualStyle & {
  id: string;
  speaker: null;
  emotion: null;
  style: "subtitle" | "speechBubble" | "narrationBox" | "titleCard" | "lowerThird";
  text: string;
  start: number;
  end: number;
  x: number;
  y: number;
  scale: number;
  scaleX?: number;
  scaleY?: number;
  rotation: number;
  rotationX?: number;
  rotationY?: number;
  perspective?: number;
  blur?: number;
  opacity: number;
  keyframes: MotionKeyframe[];
  size: number;
  color: string;
  background: string;
  backgroundColor: string;
  backgroundOpacity: number;
  align: "left" | "center" | "right";
  parentId?: string | null;
};

export type GeneratedMotionGraphic = GraphicVisualStyle & Partial<VideoEditorShaderStyle> & {
  id: string;
  kind: "group" | "rectangle" | "ellipse" | "line" | "path" | "particle" | "shader" | "video";
  label: string;
  start: number;
  end: number;
  x: number;
  y: number;
  scale: number;
  scaleX?: number;
  scaleY?: number;
  rotation: number;
  rotationX?: number;
  rotationY?: number;
  perspective?: number;
  blur?: number;
  pathProgress?: number;
  opacity: number;
  keyframes: MotionKeyframe[];
  width: number;
  height: number;
  fill: string;
  stroke: string;
  strokeWidth: number;
  cornerRadius: number;
  assetDocumentId?: string | null;
  assetName?: string | null;
  assetMimeType?: string | null;
  assetDuration?: number | null;
  sourceStart?: number;
  sourceEnd?: number | null;
  speed?: number;
  loop?: boolean;
  particleCount?: number;
  particleShape?: ParticleShape;
  particleMotion?: ParticleMotion;
  particleSeed?: number;
  particleSize?: number;
  particleSpeed?: number;
  particleGravity?: number;
  particleSpread?: number;
  particleLoop?: boolean;
  parentId?: string | null;
  clipChildren?: boolean;
  isTemplate?: boolean;
  instanceOf?: string | null;
};

export type GeneratedMotionDesign = {
  preset: MotionDesignPreset;
  captions: GeneratedMotionCaption[];
  graphics: GeneratedMotionGraphic[];
  audioCues: Array<{
    id: string;
    type: "music" | "ambience" | "sfx";
    label: string;
    start: number;
    end: number;
    volumeDb: number;
    fadeIn: number;
    fadeOut: number;
    loop: boolean;
    duckUnderDialogue: boolean;
    muted: boolean;
    sourceStart: number;
    sourceEnd: number;
    assetDuration: null;
    assetDocumentId: null;
    assetName: null;
    assetMimeType: null;
    sourcePlan: string;
    prompt: string;
  }>;
  shots: Array<{
    id: string;
    title: string;
    scene: string;
    shot: string;
    start: number;
    end: number;
    location: string;
    camera: string;
    action: string;
    dialogue: string;
    notes: string;
    x?: number;
    y?: number;
    scale?: number;
    scaleX?: number;
    scaleY?: number;
    rotation?: number;
    rotationX?: number;
    rotationY?: number;
    perspective?: number;
    blur?: number;
    opacity?: number;
    keyframes?: MotionKeyframe[];
  }>;
  markers: Array<{ id: string; time: number; label: string; color: string; notes: string }>;
};

type MotionDesignPalette = {
  background: string;
  surface: string;
  foreground: string;
  accent: string;
  accent2: string;
};

const PRESET_PALETTES: Record<MotionDesignPreset, MotionDesignPalette> = {
  "kinetic-type": {
    background: "#0b0d12",
    surface: "#171a22",
    foreground: "#f8fafc",
    accent: "#ff4d2e",
    accent2: "#9b87f5",
  },
  "swiss-grid": {
    background: "#f2f2ef",
    surface: "#ffffff",
    foreground: "#0a1e3d",
    accent: "#d4a017",
    accent2: "#d8342a",
  },
  "product-promo": {
    background: "#090b13",
    surface: "#f7f7fb",
    foreground: "#f8fafc",
    accent: "#a259ff",
    accent2: "#0acf83",
  },
  "editorial-data": {
    background: "#f7f4ed",
    surface: "#ffffff",
    foreground: "#121212",
    accent: "#e04a3f",
    accent2: "#2f6b9a",
  },
  "product-film": {
    background: "#07100f",
    surface: "#101a18",
    foreground: "#f8fafc",
    accent: "#6de3d0",
    accent2: "#c6a467",
  },
  "feature-reveal": {
    background: "#f4f5f6",
    surface: "#ffffff",
    foreground: "#1f2937",
    accent: "#5da7ff",
    accent2: "#e58562",
  },
  "product-story": {
    background: "#f5f3ee",
    surface: "#17191d",
    foreground: "#151a20",
    accent: "#f08a50",
    accent2: "#75b9c7",
  },
  "motion-design": {
    background: "#0b0d12",
    surface: "#171a22",
    foreground: "#f5efe5",
    accent: "#ff6a3d",
    accent2: "#f4c95d",
  },
  "texture-launch": {
    background: "#f3f0e8",
    surface: "#111318",
    foreground: "#0b0c0f",
    accent: "#ff4f2e",
    accent2: "#45d7ff",
  },
};

const DEFAULT_COPY: Record<MotionDesignPreset, Required<Pick<MotionDesignRequest, "headline" | "kicker" | "subhead" | "cta" | "statValue" | "statLabel">>> = {
  "kinetic-type": {
    headline: "MAKE IDEAS MOVE",
    kicker: "MANOR MOTION",
    subhead: "Editable scenes. Deterministic timing. Production-ready output.",
    cta: "Create with AI",
    statValue: "30 FPS",
    statLabel: "FRAME ACCURATE",
  },
  "swiss-grid": {
    headline: "SYSTEMS IN MOTION",
    kicker: "MANOR / 01",
    subhead: "A precise modular canvas for stories that need clarity.",
    cta: "Build the system",
    statValue: "04",
    statLabel: "SCENES",
  },
  "product-promo": {
    headline: "From prompt to polished video",
    kicker: "MANOR AI",
    subhead: "Generate the structure, refine every layer, export the result.",
    cta: "Start creating",
    statValue: "1×",
    statLabel: "WORKFLOW",
  },
  "editorial-data": {
    headline: "Momentum is compounding",
    kicker: "MANOR RESEARCH",
    subhead: "A clear data story, animated one beat at a time.",
    cta: "Explore the signal",
    statValue: "+84%",
    statLabel: "YEAR OVER YEAR",
  },
  "product-film": {
    headline: "See the product in action",
    kicker: "MANOR PRODUCT FILM",
    subhead: "Human storytelling, readable captions, and product detail in one frame.",
    cta: "Watch the full story",
    statValue: "LIVE",
    statLabel: "PRODUCT WALKTHROUGH",
  },
  "feature-reveal": {
    headline: "One feature. Every moving part.",
    kicker: "FEATURE REVEAL",
    subhead: "A clean visual system that reveals timing, flow, and impact.",
    cta: "Reveal the workflow",
    statValue: "04",
    statLabel: "CONNECTED STEPS",
  },
  "product-story": {
    headline: "From first click to finished work",
    kicker: "PRODUCT STORY",
    subhead: "Layer the interface, the problem, and the payoff into one clear narrative.",
    cta: "Build your story",
    statValue: "3×",
    statLabel: "FASTER TO VALUE",
  },
  "motion-design": {
    headline: "MAKE THE MESSAGE MOVE",
    kicker: "MOTION DESIGN",
    subhead: "Bold type, clean geometry, and a frame-accurate visual rhythm.",
    cta: "Create the motion",
    statValue: "30",
    statLabel: "FRAMES PER SECOND",
  },
  "texture-launch": {
    headline: "TEXTURE IS A LANGUAGE",
    kicker: "MANOR AI / CODED MOTION",
    subhead: "Every surface can become rhythm, depth, and transition.",
    cta: "BUILD IT IN MANOR",
    statValue: "22s",
    statLabel: "DETERMINISTIC / EDITABLE",
  },
};

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function safeText(value: string | undefined, fallback: string): string {
  const trimmed = value?.trim();
  return trimmed ? trimmed.slice(0, 180) : fallback;
}

function contrastTextColor(color: string, light = "#f8fafc", dark = "#11131a"): string {
  const normalized = color.trim().replace(/^#/, "");
  const hex = normalized.length === 3
    ? normalized.split("").map((digit) => `${digit}${digit}`).join("")
    : normalized;
  if (!/^[0-9a-f]{6}$/i.test(hex)) return light;
  const red = Number.parseInt(hex.slice(0, 2), 16);
  const green = Number.parseInt(hex.slice(2, 4), 16);
  const blue = Number.parseInt(hex.slice(4, 6), 16);
  const luminance = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255;
  return luminance > 0.56 ? dark : light;
}

function phaseTime(duration: number, normalizedTime: number): number {
  return Number((clamp(normalizedTime, 0, 1) * duration).toFixed(3));
}

function motionFrame(
  id: string,
  time: number,
  pose: MotionPose,
  easing: MotionEasing = "snappy",
  spatial: MotionPathMode = "linear",
): MotionKeyframe {
  return { id, time: Math.max(0, time), ...pose, easing, spatial };
}

function entranceKeyframes(
  id: string,
  duration: number,
  pose: MotionPose,
  delay = 0,
  offsetX = 0,
  offsetY = 8,
  startScale = 0.92,
): MotionKeyframe[] {
  const entranceEnd = Math.min(duration, delay + Math.min(0.72, Math.max(0.22, duration * 0.18)));
  return [
    motionFrame(`${id}-in`, delay, {
      ...pose,
      x: pose.x + offsetX,
      y: pose.y + offsetY,
      scale: startScale,
      opacity: 0,
    }, "hold"),
    motionFrame(`${id}-settle`, entranceEnd, pose, "snappy", "smooth"),
  ];
}

function exitKeyframe(id: string, duration: number, pose: MotionPose, offsetY = -5): MotionKeyframe {
  return motionFrame(`${id}-out`, duration, {
    ...pose,
    y: pose.y + offsetY,
    opacity: 0,
  }, "easeIn", "smooth");
}

function appendHoldAndExit(
  keyframes: MotionKeyframe[],
  id: string,
  duration: number,
  pose: MotionPose,
  offsetY = -5,
): void {
  if (duration <= 1.1) return;
  const lastEntranceTime = keyframes[keyframes.length - 1]?.time ?? 0;
  const holdTime = Math.max(lastEntranceTime, duration * 0.82);
  keyframes.push(motionFrame(`${id}-hold`, holdTime, pose, "gentle"));
  keyframes.push(exitKeyframe(id, duration, pose, offsetY));
}

function caption(
  preset: MotionDesignPreset,
  name: string,
  text: string,
  start: number,
  end: number,
  pose: MotionPose,
  options: Partial<GeneratedMotionCaption> = {},
  entranceDelay = 0,
): GeneratedMotionCaption {
  const id = `motion-${preset}-caption-${name}`;
  const localDuration = Math.max(0.05, end - start);
  const keyframes = entranceKeyframes(id, localDuration, pose, entranceDelay);
  appendHoldAndExit(keyframes, id, localDuration, pose);
  return {
    id,
    speaker: null,
    emotion: null,
    style: "titleCard",
    text,
    start,
    end,
    ...pose,
    keyframes,
    size: 64,
    color: "#ffffff",
    background: "transparent",
    backgroundColor: "#000000",
    backgroundOpacity: 0,
    align: "left",
    ...DEFAULT_CAPTION_VISUAL_STYLE,
    ...options,
  };
}

function graphic(
  preset: MotionDesignPreset,
  name: string,
  start: number,
  end: number,
  pose: MotionPose,
  options: Partial<GeneratedMotionGraphic> = {},
  entranceDelay = 0,
): GeneratedMotionGraphic {
  const id = `motion-${preset}-graphic-${name}`;
  const localDuration = Math.max(0.05, end - start);
  const isBackground = name === "background";
  const keyframes = isBackground
    ? []
    : entranceKeyframes(id, localDuration, pose, entranceDelay, 0, 3, 0.12);
  if (!isBackground) appendHoldAndExit(keyframes, id, localDuration, pose, -2);
  return {
    id,
    kind: "rectangle",
    label: name.replace(/-/g, " "),
    start,
    end,
    ...pose,
    keyframes,
    width: 20,
    height: 12,
    fill: "#ffffff",
    stroke: "#ffffff",
    strokeWidth: 0,
    cornerRadius: 0,
    ...DEFAULT_GRAPHIC_VISUAL_STYLE,
    ...options,
  };
}

function withAxisReveal(
  layer: GeneratedMotionGraphic,
  axis: "x" | "y" = "x",
  delay = 0,
  direction: 1 | -1 = 1,
): GeneratedMotionGraphic {
  const localDuration = Math.max(0.05, layer.end - layer.start);
  const settle = Math.min(localDuration * 0.45, delay + Math.max(0.28, localDuration * 0.12));
  const startOffset = axis === "x" ? (layer.width * 0.48 * direction) : 0;
  const startYOffset = axis === "y" ? (layer.height * 0.48 * direction) : 0;
  const pose: MotionPose = {
    x: layer.x,
    y: layer.y,
    scale: layer.scale,
    scaleX: 1,
    scaleY: 1,
    rotation: layer.rotation,
    opacity: layer.opacity,
  };
  return {
    ...layer,
    keyframes: [
      motionFrame(`${layer.id}-axis-in`, delay, {
        ...pose,
        x: layer.x - startOffset,
        y: layer.y - startYOffset,
        scaleX: axis === "x" ? 0.02 : 1,
        scaleY: axis === "y" ? 0.02 : 1,
        opacity: 1,
      }, "hold"),
      motionFrame(`${layer.id}-axis-settle`, settle, pose, "snappy", "smooth"),
      motionFrame(`${layer.id}-axis-hold`, Math.max(settle, localDuration * 0.82), pose, "gentle"),
      motionFrame(`${layer.id}-axis-out`, localDuration, {
        ...pose,
        x: layer.x + startOffset * 0.18,
        y: layer.y + startYOffset * 0.18,
        opacity: 0,
      }, "easeIn", "smooth"),
    ],
  };
}

function withCameraDrift(
  layer: GeneratedMotionGraphic,
  x: number,
  y: number,
  scale: number,
): GeneratedMotionGraphic {
  const localDuration = Math.max(0.05, layer.end - layer.start);
  const pose: MotionPose = {
    x: layer.x,
    y: layer.y,
    scale: layer.scale,
    rotation: layer.rotation,
    opacity: layer.opacity,
  };
  return {
    ...layer,
    keyframes: [
      motionFrame(`${layer.id}-drift-in`, 0, pose, "linear"),
      motionFrame(`${layer.id}-drift-out`, localDuration, {
        ...pose,
        x: layer.x + x,
        y: layer.y + y,
        scale: layer.scale * scale,
      }, "gentle", "smooth"),
    ],
  };
}

function particleBurst(
  preset: MotionDesignPreset,
  duration: number,
  startAt: number,
  endAt: number,
  origin: { x: number; y: number },
  colors: string[],
  count = 12,
  zIndex = 90,
): GeneratedMotionGraphic[] {
  const start = phaseTime(duration, startAt);
  const end = phaseTime(duration, endAt);
  const localDuration = Math.max(0.2, end - start);
  return Array.from({ length: count }, (_, index) => {
    const angle = ((index * 137.5 - 90) * Math.PI) / 180;
    const distance = 7 + ((index * 29) % 9);
    const width = 0.45 + ((index * 17) % 5) * 0.12;
    const targetX = origin.x + Math.cos(angle) * distance;
    const targetY = origin.y + Math.sin(angle) * distance + 3.5;
    const particle = graphic(preset, `particle-${index + 1}`, start, end, { x: origin.x, y: origin.y, scale: 0.2, rotation: index * 31, opacity: 0 }, {
      kind: index % 4 === 0 ? "rectangle" : "ellipse",
      width,
      height: width * (index % 4 === 0 ? 1.8 : 1.78),
      fill: colors[index % colors.length],
      fillSecondary: colors[(index + 1) % colors.length],
      effect: "glow",
      effectStrength: 0.38,
      blendMode: "screen",
      cornerRadius: 40,
      zIndex,
    });
    particle.keyframes = [
      motionFrame(`${particle.id}-hidden`, 0, { x: origin.x, y: origin.y, scale: 0.2, rotation: index * 31, opacity: 0 }, "hold"),
      motionFrame(`${particle.id}-pop`, localDuration * 0.1, { x: origin.x, y: origin.y, scale: 1, rotation: index * 31, opacity: 0.95 }, "snappy"),
      motionFrame(`${particle.id}-flight`, localDuration * 0.58, { x: origin.x + (targetX - origin.x) * 0.72, y: origin.y + (targetY - origin.y) * 0.5 - 2.5, scale: 0.82, rotation: index * 79, opacity: 0.78 }, "linear", "smooth"),
      motionFrame(`${particle.id}-dead`, localDuration, { x: targetX, y: targetY, scale: 0.15, rotation: index * 143, opacity: 0 }, "linear", "smooth"),
    ];
    return particle;
  });
}

function clickRipple(
  preset: MotionDesignPreset,
  duration: number,
  startAt: number,
  origin: { x: number; y: number },
  color: string,
  index: number,
  zIndex = 45,
): GeneratedMotionGraphic {
  const start = phaseTime(duration, startAt + index * 0.025);
  const end = phaseTime(duration, startAt + 0.18 + index * 0.04);
  const ripple = graphic(preset, `click-ripple-${index + 1}`, start, end, { x: origin.x, y: origin.y, scale: 0.1, rotation: 0, opacity: 0 }, {
    kind: "ellipse",
    width: 7 + index * 3.2,
    height: 12.4 + index * 5.7,
    fill: "rgba(0,0,0,0)",
    stroke: color,
    strokeWidth: 1.2,
    effect: "glow",
    effectStrength: 0.55,
    zIndex,
  });
  const localDuration = Math.max(0.1, ripple.end - ripple.start);
  ripple.keyframes = [
    motionFrame(`${ripple.id}-hidden`, 0, { x: origin.x, y: origin.y, scale: 0.1, rotation: 0, opacity: 0 }, "hold"),
    motionFrame(`${ripple.id}-attack`, localDuration * 0.12, { x: origin.x, y: origin.y, scale: 0.22, rotation: 0, opacity: 0.82 }, "snappy"),
    motionFrame(`${ripple.id}-expand`, localDuration, { x: origin.x, y: origin.y, scale: 1, rotation: 0, opacity: 0 }, "easeOut"),
  ];
  return ripple;
}

function opticalTransition(
  preset: MotionDesignPreset,
  duration: number,
  name: string,
  startAt: number,
  endAt: number,
  effect: GraphicVisualStyle["effect"],
  fromX: number,
  toX: number,
  peakOpacity: number,
  strength: number,
  zIndex: number,
): GeneratedMotionGraphic {
  const start = phaseTime(duration, startAt);
  const end = phaseTime(duration, endAt);
  const localDuration = Math.max(0.2, end - start);
  const layer = graphic(preset, name, start, end, {
    x: fromX,
    y: 50,
    scale: 1,
    rotation: 0,
    opacity: 0,
  }, {
    width: effect === "anamorphic" ? 118 : 142,
    height: effect === "anamorphic" ? 32 : 126,
    fill: "rgba(0,0,0,0)",
    fillSecondary: "rgba(255,154,83,0)",
    blendMode: "screen",
    effect,
    effectStrength: strength,
    zIndex,
  });
  layer.keyframes = [
    motionFrame(`${layer.id}-hidden`, 0, { x: fromX, y: 50, scale: 0.96, rotation: 0, opacity: 0 }, "hold"),
    motionFrame(`${layer.id}-attack`, localDuration * 0.22, { x: fromX + (toX - fromX) * 0.24, y: 49, scale: 1.03, rotation: 0, opacity: peakOpacity * 0.72 }, "easeIn", "smooth"),
    motionFrame(`${layer.id}-peak`, localDuration * 0.52, { x: fromX + (toX - fromX) * 0.58, y: 50, scale: 1.1, rotation: 0, opacity: peakOpacity }, "easeInOut", "smooth"),
    motionFrame(`${layer.id}-release`, localDuration, { x: toX, y: 51, scale: 1.2, rotation: 0, opacity: 0 }, "easeOut", "smooth"),
  ];
  return layer;
}

function baseSceneData(preset: MotionDesignPreset, duration: number, palette: MotionDesignPalette): Pick<GeneratedMotionDesign, "shots" | "markers"> {
  const boundaries = preset === "texture-launch"
    ? [0, 0.14, 0.38, 0.7, 1]
    : preset === "motion-design"
    ? [0, 0.2, 0.58, 0.83, 1]
    : [0, 0.24, 0.68, 0.86, 1];
  const titles = preset === "texture-launch"
    ? ["Hook", "Texture rip", "Warp", "Resolve"]
    : ["Hook", "Build", "Proof", "Resolve"];
  const shots = titles.map((title, index) => ({
    id: `motion-${preset}-scene-${index + 1}`,
    title,
    scene: `Scene ${index + 1}`,
    shot: `${preset} / ${String(index + 1).padStart(2, "0")}`,
    start: phaseTime(duration, boundaries[index]),
    end: phaseTime(duration, boundaries[index + 1]),
    location: "Motion canvas",
    camera: preset === "texture-launch"
      ? ["Editorial lockup", "Rapid material cuts", "Optical warp field", "Branded resolve"][index]
      : preset === "motion-design"
      ? ["Editorial impact frame", "Layered editor assembly", "Split-frame proof", "Brand-color resolve"][index]
      : index === 1 ? "Layered build" : index === 2 ? "Detail push" : "Locked graphic frame",
    action: `${title} phase of the generated ${preset} composition`,
    dialogue: "",
    notes: "Generated by Manor's deterministic native motion-design preset.",
  }));
  return {
    shots,
    markers: shots.map((shot, index) => ({
      id: `motion-${preset}-marker-${index + 1}`,
      time: shot.start,
      label: shot.title,
      color: index % 2 === 0 ? palette.accent : palette.accent2,
      notes: shot.action,
    })),
  };
}

function buildKineticType(
  duration: number,
  palette: MotionDesignPalette,
  copy: typeof DEFAULT_COPY[MotionDesignPreset],
): Pick<GeneratedMotionDesign, "captions" | "graphics"> {
  const sceneEnd = phaseTime(duration, 0.86);
  const captions: GeneratedMotionCaption[] = [
    caption("kinetic-type", "kicker", copy.kicker, phaseTime(duration, 0.02), phaseTime(duration, 0.25), { x: 9, y: 16, scale: 1, rotation: 0, opacity: 1 }, { size: 24, color: palette.accent, align: "left" }),
    caption("kinetic-type", "headline", copy.headline, phaseTime(duration, 0.06), phaseTime(duration, 0.56), { x: 9, y: 43, scale: 1, rotation: 0, opacity: 1 }, { size: 94, color: palette.foreground, align: "left" }, 0.08),
    caption("kinetic-type", "stat", copy.statValue, phaseTime(duration, 0.3), phaseTime(duration, 0.72), { x: 74, y: 23, scale: 1, rotation: -5, opacity: 1 }, { size: 74, color: palette.background, backgroundColor: palette.accent, background: palette.accent, backgroundOpacity: 1, align: "center" }, 0.12),
    caption("kinetic-type", "stat-label", copy.statLabel, phaseTime(duration, 0.34), phaseTime(duration, 0.72), { x: 75, y: 35, scale: 1, rotation: 0, opacity: 1 }, { size: 20, color: palette.foreground, align: "center" }, 0.18),
    caption("kinetic-type", "subhead", copy.subhead, phaseTime(duration, 0.38), sceneEnd, { x: 10, y: 70, scale: 1, rotation: 0, opacity: 1 }, { size: 34, color: palette.foreground, align: "left" }, 0.12),
    caption("kinetic-type", "cta", copy.cta, phaseTime(duration, 0.7), duration, { x: 50, y: 51, scale: 1, rotation: 0, opacity: 1 }, { style: "lowerThird", size: 54, color: palette.background, backgroundColor: palette.foreground, background: palette.foreground, backgroundOpacity: 1, align: "center" }),
  ];
  const graphics: GeneratedMotionGraphic[] = [
    graphic("kinetic-type", "background", 0, duration, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 1 }, { width: 100, height: 100, fill: palette.background }),
    graphic("kinetic-type", "accent-rule", phaseTime(duration, 0.02), sceneEnd, { x: 22, y: 22, scale: 1, rotation: 0, opacity: 1 }, { width: 27, height: 0.7, fill: palette.accent }),
    graphic("kinetic-type", "violet-block", phaseTime(duration, 0.12), sceneEnd, { x: 89, y: 77, scale: 1, rotation: 12, opacity: 0.9 }, { width: 23, height: 27, fill: palette.accent2, cornerRadius: 12 }, 0.1),
    graphic("kinetic-type", "micro-dot-1", phaseTime(duration, 0.16), sceneEnd, { x: 59, y: 18, scale: 1, rotation: 0, opacity: 1 }, { kind: "ellipse", width: 3.2, height: 5.6, fill: palette.accent }),
    graphic("kinetic-type", "micro-dot-2", phaseTime(duration, 0.2), sceneEnd, { x: 64, y: 18, scale: 1, rotation: 0, opacity: 0.75 }, { kind: "ellipse", width: 2.2, height: 3.9, fill: palette.foreground }, 0.08),
    graphic("kinetic-type", "footer-rule", phaseTime(duration, 0.34), sceneEnd, { x: 28, y: 84, scale: 1, rotation: 0, opacity: 0.5 }, { width: 38, height: 0.35, fill: palette.foreground }),
    graphic("kinetic-type", "wipe", phaseTime(duration, 0.82), duration, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 1 }, { width: 100, height: 100, fill: palette.accent2 }, 0),
    graphic("kinetic-type", "outro-panel", phaseTime(duration, 0.84), duration, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 1 }, { width: 100, height: 100, fill: palette.background }, 0.05),
  ];
  return { captions, graphics };
}

function buildSwissGrid(
  duration: number,
  palette: MotionDesignPalette,
  copy: typeof DEFAULT_COPY[MotionDesignPreset],
): Pick<GeneratedMotionDesign, "captions" | "graphics"> {
  const gridEnd = phaseTime(duration, 0.88);
  const graphics: GeneratedMotionGraphic[] = [
    graphic("swiss-grid", "background", 0, duration, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 1 }, { width: 100, height: 100, fill: palette.background }),
  ];
  [10, 26, 42, 58, 74, 90].forEach((x, index) => {
    graphics.push(graphic("swiss-grid", `column-${index + 1}`, phaseTime(duration, 0.01 + index * 0.008), gridEnd, { x, y: 50, scale: 1, rotation: 0, opacity: 0.22 }, { width: 0.22, height: 100, fill: palette.foreground }, index * 0.025));
  });
  [16, 37, 58, 79].forEach((y, index) => {
    graphics.push(graphic("swiss-grid", `row-${index + 1}`, phaseTime(duration, 0.02 + index * 0.01), gridEnd, { x: 50, y, scale: 1, rotation: 0, opacity: 0.18 }, { width: 100, height: 0.25, fill: palette.foreground }, index * 0.03));
  });
  graphics.push(
    graphic("swiss-grid", "accent-square", phaseTime(duration, 0.08), gridEnd, { x: 82, y: 25, scale: 1, rotation: 0, opacity: 1 }, { width: 15.5, height: 22, fill: palette.accent }),
    graphic("swiss-grid", "accent-circle", phaseTime(duration, 0.22), gridEnd, { x: 66, y: 70, scale: 1, rotation: 0, opacity: 1 }, { kind: "ellipse", width: 13, height: 23, fill: palette.accent2 }, 0.12),
    graphic("swiss-grid", "outro", phaseTime(duration, 0.86), duration, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 1 }, { width: 100, height: 100, fill: palette.foreground }),
  );
  const captions: GeneratedMotionCaption[] = [
    caption("swiss-grid", "kicker", copy.kicker, phaseTime(duration, 0.03), gridEnd, { x: 10, y: 10, scale: 1, rotation: 0, opacity: 1 }, { size: 24, color: palette.foreground, align: "left" }),
    caption("swiss-grid", "headline", copy.headline, phaseTime(duration, 0.08), phaseTime(duration, 0.62), { x: 10, y: 43, scale: 1, rotation: 0, opacity: 1 }, { size: 88, color: palette.foreground, align: "left" }, 0.1),
    caption("swiss-grid", "index", copy.statValue, phaseTime(duration, 0.18), gridEnd, { x: 82, y: 25, scale: 1, rotation: 0, opacity: 1 }, { size: 76, color: palette.foreground, align: "center" }, 0.16),
    caption("swiss-grid", "subhead", copy.subhead, phaseTime(duration, 0.38), gridEnd, { x: 10, y: 73, scale: 1, rotation: 0, opacity: 1 }, { size: 31, color: palette.foreground, align: "left" }),
    caption("swiss-grid", "cta", copy.cta, phaseTime(duration, 0.88), duration, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 1 }, { size: 58, color: palette.background, align: "center" }),
  ];
  return { captions, graphics };
}

function buildProductPromo(
  duration: number,
  palette: MotionDesignPalette,
  copy: typeof DEFAULT_COPY[MotionDesignPreset],
): Pick<GeneratedMotionDesign, "captions" | "graphics"> {
  const demoStart = phaseTime(duration, 0.18);
  const demoEnd = phaseTime(duration, 0.82);
  const surfaceText = contrastTextColor(palette.surface);
  const accentText = contrastTextColor(palette.accent);
  const accent2Text = contrastTextColor(palette.accent2);
  const graphics: GeneratedMotionGraphic[] = [
    graphic("product-promo", "background", 0, duration, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 1 }, { width: 100, height: 100, fill: palette.background }),
    graphic("product-promo", "glow", 0, duration, { x: 78, y: 20, scale: 1, rotation: 0, opacity: 0.18 }, { kind: "ellipse", width: 48, height: 76, fill: palette.accent, cornerRadius: 100 }),
    graphic("product-promo", "window-shadow", demoStart, demoEnd, { x: 51.5, y: 53, scale: 1, rotation: -1, opacity: 0.38 }, { width: 82, height: 68, fill: "#000000", cornerRadius: 5 }),
    graphic("product-promo", "window", demoStart, demoEnd, { x: 50, y: 50, scale: 1, rotation: -1, opacity: 1 }, { width: 82, height: 68, fill: palette.surface, cornerRadius: 5 }, 0.06),
    graphic("product-promo", "toolbar", phaseTime(duration, 0.23), demoEnd, { x: 50, y: 21, scale: 1, rotation: -1, opacity: 1 }, { width: 82, height: 8.5, fill: "#e7e7ef", cornerRadius: 4 }, 0.08),
    graphic("product-promo", "sidebar", phaseTime(duration, 0.26), demoEnd, { x: 16.5, y: 52, scale: 1, rotation: -1, opacity: 1 }, { width: 13, height: 55, fill: "#ececf3", cornerRadius: 3 }, 0.12),
    graphic("product-promo", "canvas-card-a", phaseTime(duration, 0.3), demoEnd, { x: 42, y: 49, scale: 1, rotation: -1, opacity: 1 }, { width: 27, height: 25, fill: palette.accent, cornerRadius: 9 }, 0.14),
    graphic("product-promo", "canvas-card-b", phaseTime(duration, 0.34), demoEnd, { x: 68, y: 56, scale: 1, rotation: -1, opacity: 1 }, { width: 18, height: 34, fill: palette.accent2, cornerRadius: 9 }, 0.2),
    graphic("product-promo", "cursor-one", phaseTime(duration, 0.4), demoEnd, { x: 37, y: 38, scale: 1, rotation: 0, opacity: 1 }, { kind: "ellipse", width: 2.4, height: 4.2, fill: "#1abcfe" }, 0.18),
    graphic("product-promo", "cursor-two", phaseTime(duration, 0.44), demoEnd, { x: 72, y: 70, scale: 1, rotation: 0, opacity: 1 }, { kind: "ellipse", width: 2.4, height: 4.2, fill: "#f24e1e" }, 0.24),
    graphic("product-promo", "outro", phaseTime(duration, 0.8), duration, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 1 }, { width: 100, height: 100, fill: palette.background }),
    graphic("product-promo", "outro-orb", phaseTime(duration, 0.82), duration, { x: 50, y: 44, scale: 1, rotation: 0, opacity: 0.65 }, { kind: "ellipse", width: 11, height: 19.5, fill: palette.accent }, 0.06),
  ];
  const captions: GeneratedMotionCaption[] = [
    caption("product-promo", "kicker", copy.kicker, phaseTime(duration, 0.02), phaseTime(duration, 0.2), { x: 50, y: 34, scale: 1, rotation: 0, opacity: 1 }, { size: 24, color: palette.accent2, align: "center" }),
    caption("product-promo", "headline", copy.headline, phaseTime(duration, 0.03), phaseTime(duration, 0.2), { x: 50, y: 49, scale: 1, rotation: 0, opacity: 1 }, { size: 70, color: palette.foreground, align: "center" }, 0.05),
    caption("product-promo", "window-title", copy.subhead, phaseTime(duration, 0.3), demoEnd, { x: 29, y: 35, scale: 1, rotation: -1, opacity: 1 }, { size: 31, color: surfaceText, align: "left" }, 0.14),
    caption("product-promo", "window-stat", copy.statValue, phaseTime(duration, 0.42), demoEnd, { x: 42, y: 51, scale: 1, rotation: -1, opacity: 1 }, { size: 52, color: accentText, align: "center" }, 0.14),
    caption("product-promo", "window-label", copy.statLabel, phaseTime(duration, 0.48), demoEnd, { x: 68, y: 57, scale: 1, rotation: -1, opacity: 1 }, { size: 20, color: accent2Text, align: "center" }, 0.18),
    caption("product-promo", "cta", copy.cta, phaseTime(duration, 0.82), duration, { x: 50, y: 61, scale: 1, rotation: 0, opacity: 1 }, { style: "lowerThird", size: 44, color: palette.foreground, backgroundColor: palette.surface, background: palette.surface, backgroundOpacity: 0.12, align: "center" }, 0.12),
  ];
  return { captions, graphics };
}

function buildEditorialData(
  duration: number,
  palette: MotionDesignPalette,
  copy: typeof DEFAULT_COPY[MotionDesignPreset],
): Pick<GeneratedMotionDesign, "captions" | "graphics"> {
  const chartStart = phaseTime(duration, 0.24);
  const chartEnd = phaseTime(duration, 0.86);
  const graphics: GeneratedMotionGraphic[] = [
    graphic("editorial-data", "background", 0, duration, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 1 }, { width: 100, height: 100, fill: palette.background }),
    graphic("editorial-data", "chart-surface", chartStart, chartEnd, { x: 60, y: 58, scale: 1, rotation: 0, opacity: 1 }, { width: 68, height: 60, fill: palette.surface, cornerRadius: 4 }),
    graphic("editorial-data", "axis-x", phaseTime(duration, 0.27), chartEnd, { x: 60, y: 78, scale: 1, rotation: 0, opacity: 0.55 }, { width: 58, height: 0.35, fill: palette.foreground }),
    graphic("editorial-data", "axis-y", phaseTime(duration, 0.28), chartEnd, { x: 31, y: 56, scale: 1, rotation: 0, opacity: 0.55 }, { width: 0.22, height: 44, fill: palette.foreground }),
  ];
  const barHeights = [12, 18, 24, 31, 39, 47];
  barHeights.forEach((height, index) => {
    graphics.push(graphic("editorial-data", `bar-${index + 1}`, phaseTime(duration, 0.3 + index * 0.045), chartEnd, { x: 38 + index * 8.7, y: 78 - height / 2, scale: 1, rotation: 0, opacity: 0.92 }, { width: 5.2, height, fill: index === barHeights.length - 1 ? palette.accent : palette.accent2, cornerRadius: 12 }, index * 0.05));
  });
  graphics.push(
    graphic("editorial-data", "highlight", phaseTime(duration, 0.58), chartEnd, { x: 81.5, y: 31, scale: 1, rotation: 0, opacity: 1 }, { kind: "ellipse", width: 4.5, height: 8, fill: palette.accent }, 0.08),
    graphic("editorial-data", "outro", phaseTime(duration, 0.84), duration, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 1 }, { width: 100, height: 100, fill: palette.foreground }),
  );
  const captions: GeneratedMotionCaption[] = [
    caption("editorial-data", "kicker", copy.kicker, phaseTime(duration, 0.02), chartEnd, { x: 9, y: 12, scale: 1, rotation: 0, opacity: 1 }, { size: 22, color: palette.accent, align: "left" }),
    caption("editorial-data", "headline", copy.headline, phaseTime(duration, 0.04), phaseTime(duration, 0.34), { x: 9, y: 31, scale: 1, rotation: 0, opacity: 1 }, { size: 72, color: palette.foreground, align: "left" }, 0.08),
    caption("editorial-data", "subhead", copy.subhead, phaseTime(duration, 0.17), phaseTime(duration, 0.48), { x: 9, y: 49, scale: 1, rotation: 0, opacity: 1 }, { size: 28, color: palette.foreground, align: "left" }, 0.1),
    caption("editorial-data", "stat", copy.statValue, phaseTime(duration, 0.5), chartEnd, { x: 18, y: 68, scale: 1, rotation: 0, opacity: 1 }, { size: 66, color: palette.accent, align: "left" }, 0.08),
    caption("editorial-data", "stat-label", copy.statLabel, phaseTime(duration, 0.54), chartEnd, { x: 18, y: 80, scale: 1, rotation: 0, opacity: 1 }, { size: 19, color: palette.foreground, align: "left" }, 0.16),
    caption("editorial-data", "cta", copy.cta, phaseTime(duration, 0.86), duration, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 1 }, { size: 52, color: palette.background, align: "center" }),
  ];
  return { captions, graphics };
}

function buildProductFilm(
  duration: number,
  palette: MotionDesignPalette,
  copy: typeof DEFAULT_COPY[MotionDesignPreset],
): Pick<GeneratedMotionDesign, "captions" | "graphics"> {
  const overlayStart = phaseTime(duration, 0.08);
  const overlayEnd = phaseTime(duration, 0.88);
  const panelText = contrastTextColor(palette.surface);
  const graphics: GeneratedMotionGraphic[] = [
    graphic("product-film", "background", 0, duration, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 0.42 }, {
      width: 100,
      height: 100,
      fill: palette.background,
      fillType: "linear",
      fillSecondary: "rgba(7, 16, 15, 0.12)",
      gradientAngle: 95,
      effect: "vignette",
      effectStrength: 0.68,
      zIndex: -100,
    }),
    withCameraDrift(graphic("product-film", "ambient-glow", 0, overlayEnd, { x: 24, y: 35, scale: 1, rotation: 0, opacity: 0.22 }, {
      kind: "ellipse",
      width: 56,
      height: 88,
      fill: palette.accent,
      fillType: "radial",
      fillSecondary: "rgba(109, 227, 208, 0)",
      blur: 38,
      blendMode: "screen",
      effect: "glow",
      effectStrength: 0.48,
      zIndex: -80,
    }), 2.5, -1.5, 1.08),
    withAxisReveal(graphic("product-film", "brand-rule", overlayStart, overlayEnd, { x: 18, y: 11, scale: 1, rotation: 0, opacity: 0.82 }, {
      kind: "line",
      width: 24,
      height: 0.22,
      fill: palette.accent,
      zIndex: 20,
    }), "x", 0.02),
    graphic("product-film", "presenter-ring", phaseTime(duration, 0.14), overlayEnd, { x: 27, y: 54, scale: 1, rotation: 0, opacity: 0.34 }, {
      kind: "ellipse",
      width: 35,
      height: 62,
      fill: "rgba(0, 0, 0, 0)",
      stroke: palette.accent,
      strokeWidth: 0.7,
      shadowColor: "rgba(109, 227, 208, 0.28)",
      shadowBlur: 22,
      zIndex: 5,
    }, 0.04),
    graphic("product-film", "code-panel", phaseTime(duration, 0.16), overlayEnd, { x: 77, y: 49, scale: 1, rotation: 1.2, opacity: 0.94 }, {
      width: 34,
      height: 63,
      fill: "#0b1514",
      fillType: "linear",
      fillSecondary: palette.surface,
      gradientAngle: 145,
      stroke: "rgba(109, 227, 208, 0.55)",
      strokeWidth: 0.65,
      cornerRadius: 5,
      shadowColor: "rgba(0, 0, 0, 0.5)",
      shadowBlur: 34,
      shadowOffsetY: 16,
      effect: "glass",
      effectStrength: 0.64,
      zIndex: 30,
    }, 0.08),
    graphic("product-film", "code-header", phaseTime(duration, 0.18), overlayEnd, { x: 77, y: 21.2, scale: 1, rotation: 1.2, opacity: 0.95 }, {
      width: 34,
      height: 7.5,
      fill: "rgba(255, 255, 255, 0.06)",
      cornerRadius: 5,
      effect: "scanlines",
      effectStrength: 0.18,
      zIndex: 31,
    }, 0.1),
    graphic("product-film", "code-status", phaseTime(duration, 0.2), overlayEnd, { x: 88.5, y: 21.2, scale: 1, rotation: 1.2, opacity: 1 }, {
      kind: "ellipse",
      width: 1.2,
      height: 2.1,
      fill: palette.accent,
      shadowColor: "rgba(109, 227, 208, 0.45)",
      shadowBlur: 10,
      effect: "glow",
      effectStrength: 0.64,
      zIndex: 34,
    }, 0.12),
    withAxisReveal(graphic("product-film", "code-line-1", phaseTime(duration, 0.23), overlayEnd, { x: 70, y: 33, scale: 1, rotation: 1.2, opacity: 0.94 }, { width: 15, height: 1.05, fill: palette.accent, cornerRadius: 12, zIndex: 33 }), "x", 0.08),
    withAxisReveal(graphic("product-film", "code-line-2", phaseTime(duration, 0.27), overlayEnd, { x: 77, y: 40, scale: 1, rotation: 1.2, opacity: 0.82 }, { width: 21, height: 1.05, fill: palette.accent2, cornerRadius: 12, zIndex: 33 }), "x", 0.12),
    withAxisReveal(graphic("product-film", "code-line-3", phaseTime(duration, 0.31), overlayEnd, { x: 72, y: 47, scale: 1, rotation: 1.2, opacity: 0.84 }, { width: 14, height: 1.05, fill: "#8fb8ff", cornerRadius: 12, zIndex: 33 }), "x", 0.16),
    withAxisReveal(graphic("product-film", "code-line-4", phaseTime(duration, 0.35), overlayEnd, { x: 79, y: 54, scale: 1, rotation: 1.2, opacity: 0.62 }, { width: 20, height: 1.05, fill: palette.foreground, cornerRadius: 12, zIndex: 33 }), "x", 0.2),
    withAxisReveal(graphic("product-film", "code-line-5", phaseTime(duration, 0.39), overlayEnd, { x: 73.5, y: 61, scale: 1, rotation: 1.2, opacity: 0.5 }, { width: 13, height: 1.05, fill: palette.accent2, cornerRadius: 12, zIndex: 33 }), "x", 0.24),
    graphic("product-film", "agent-cursor", phaseTime(duration, 0.43), overlayEnd, { x: 85, y: 68, scale: 1, rotation: -10, opacity: 0.95 }, {
      kind: "path",
      width: 2.2,
      height: 4.5,
      fill: palette.foreground,
      stroke: palette.background,
      strokeWidth: 0.5,
      pathData: "M 10 5 L 88 48 L 55 58 L 42 94 Z",
      shadowColor: "rgba(0, 0, 0, 0.4)",
      shadowBlur: 8,
      effect: "chromatic",
      effectStrength: 0.32,
      zIndex: 36,
    }, 0.1),
    graphic("product-film", "caption-glass", phaseTime(duration, 0.34), overlayEnd, { x: 50, y: 89, scale: 1, rotation: 0, opacity: 0.9 }, {
      width: 72,
      height: 11,
      fill: "rgba(7, 16, 15, 0.72)",
      stroke: "rgba(255, 255, 255, 0.16)",
      strokeWidth: 0.5,
      cornerRadius: 14,
      shadowColor: "rgba(0, 0, 0, 0.35)",
      shadowBlur: 22,
      shadowOffsetY: 8,
      effect: "glass",
      effectStrength: 0.52,
      zIndex: 50,
    }, 0.08),
    graphic("product-film", "outro", phaseTime(duration, 0.86), duration, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 0.98 }, {
      width: 100,
      height: 100,
      fill: palette.background,
      fillType: "linear",
      fillSecondary: "#132b27",
      gradientAngle: 135,
      effect: "grain",
      effectStrength: 0.34,
      zIndex: 500,
    }),
  ];
  const codeSweep = graphic("product-film", "code-sheen", phaseTime(duration, 0.45), phaseTime(duration, 0.72), { x: 62, y: 49, scale: 1, rotation: 8, opacity: 0 }, {
    width: 5.5,
    height: 65,
    fill: "rgba(255,255,255,0)",
    fillType: "linear",
    fillSecondary: "rgba(109,227,208,0.68)",
    gradientAngle: 0,
    blur: 8,
    blendMode: "screen",
    zIndex: 38,
  });
  const codeSweepDuration = Math.max(0.1, codeSweep.end - codeSweep.start);
  codeSweep.keyframes = [
    motionFrame(`${codeSweep.id}-start`, 0, { x: 61, y: 49, scale: 1, scaleX: 0.35, scaleY: 1, rotation: 8, opacity: 0 }, "hold"),
    motionFrame(`${codeSweep.id}-show`, codeSweepDuration * 0.18, { x: 66, y: 49, scale: 1, scaleX: 1, scaleY: 1, rotation: 8, opacity: 0.24 }, "easeOut"),
    motionFrame(`${codeSweep.id}-travel`, codeSweepDuration * 0.84, { x: 87, y: 49, scale: 1, scaleX: 1, scaleY: 1, rotation: 8, opacity: 0.18 }, "linear", "smooth"),
    motionFrame(`${codeSweep.id}-out`, codeSweepDuration, { x: 91, y: 49, scale: 1, scaleX: 0.35, scaleY: 1, rotation: 8, opacity: 0 }, "easeIn"),
  ];
  graphics.push(codeSweep);
  const captions: GeneratedMotionCaption[] = [
    caption("product-film", "kicker", copy.kicker, overlayStart, overlayEnd, { x: 6, y: 8, scale: 1, rotation: 0, opacity: 1 }, { size: 14, color: palette.accent, align: "left", fontFamily: "mono", fontWeight: 700, letterSpacing: 2.4, textTransform: "uppercase", maxWidth: 48, reveal: "characters", revealDuration: 0.55, zIndex: 80 }),
    caption("product-film", "headline", copy.headline, phaseTime(duration, 0.09), phaseTime(duration, 0.38), { x: 6, y: 20, scale: 1, rotation: 0, opacity: 1 }, { size: 58, color: palette.foreground, align: "left", fontFamily: "display", fontWeight: 800, letterSpacing: -1.5, lineHeight: 0.95, maxWidth: 58, reveal: "words", revealDuration: 0.5, textShadowColor: "rgba(0, 0, 0, 0.35)", textShadowBlur: 18, zIndex: 80 }, 0.04),
    caption("product-film", "panel-title", "AGENT RUNTIME / LIVE", phaseTime(duration, 0.2), overlayEnd, { x: 64, y: 21, scale: 1, rotation: 1.2, opacity: 0.68 }, { size: 10, color: panelText, align: "left", fontFamily: "mono", fontWeight: 600, letterSpacing: 1.1, maxWidth: 24, zIndex: 36 }, 0.08),
    caption("product-film", "panel-stat", copy.statValue, phaseTime(duration, 0.26), overlayEnd, { x: 77, y: 70, scale: 1, rotation: 1.2, opacity: 1 }, { size: 32, color: palette.accent, align: "center", fontFamily: "display", fontWeight: 800, letterSpacing: -1, maxWidth: 22, zIndex: 36 }, 0.12),
    caption("product-film", "panel-label", copy.statLabel, phaseTime(duration, 0.3), overlayEnd, { x: 77, y: 77, scale: 1, rotation: 1.2, opacity: 0.72 }, { size: 10, color: panelText, align: "center", fontFamily: "mono", fontWeight: 700, letterSpacing: 1.2, textTransform: "uppercase", maxWidth: 26, zIndex: 36 }, 0.16),
    caption("product-film", "subtitle", copy.subhead, phaseTime(duration, 0.34), overlayEnd, { x: 50, y: 89, scale: 1, rotation: 0, opacity: 1 }, { style: "narrationBox", size: 21, color: "#f8fafc", backgroundColor: palette.surface, background: palette.surface, backgroundOpacity: 0, align: "center", fontFamily: "sans", fontWeight: 600, maxWidth: 64, reveal: "words", revealDuration: 0.65, zIndex: 70 }, 0.08),
    caption("product-film", "cta", copy.cta, phaseTime(duration, 0.88), duration, { x: 50, y: 52, scale: 1, rotation: 0, opacity: 1 }, { style: "lowerThird", size: 50, color: palette.foreground, backgroundColor: palette.accent, background: palette.accent, backgroundOpacity: 0.14, align: "center", fontFamily: "display", fontWeight: 800, letterSpacing: -1.1, maxWidth: 66, reveal: "wipe", revealDuration: 0.34, zIndex: 600 }),
  ];
  return { captions, graphics };
}

function buildFeatureReveal(
  duration: number,
  palette: MotionDesignPalette,
  copy: typeof DEFAULT_COPY[MotionDesignPreset],
): Pick<GeneratedMotionDesign, "captions" | "graphics"> {
  const revealEnd = phaseTime(duration, 0.9);
  const chartStart = phaseTime(duration, 0.12);
  const chartEnd = phaseTime(duration, 0.86);
  const background = graphic("feature-reveal", "background", 0, duration, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 1 }, {
    width: 100,
    height: 100,
    fill: palette.background,
    fillType: "linear",
    fillSecondary: "#eceff2",
    gradientAngle: 118,
    effect: "grain",
    effectStrength: 0.34,
    zIndex: -100,
  });
  const topGlow = withCameraDrift(graphic("feature-reveal", "ambient-glow", 0, chartEnd, { x: 82, y: 12, scale: 1, rotation: 0, opacity: 0.2 }, {
    kind: "ellipse",
    width: 38,
    height: 50,
    fill: "#ffffff",
    fillType: "radial",
    fillSecondary: palette.accent,
    blur: 28,
    blendMode: "screen",
    effect: "glow",
    effectStrength: 0.55,
    zIndex: -90,
  }), -2, 1.5, 1.08);
  const panel = graphic("feature-reveal", "timeline-surface", chartStart, chartEnd, { x: 50, y: 56, scale: 1, rotation: 0, opacity: 0.98 }, {
    width: 88,
    height: 62,
    fill: palette.surface,
    stroke: "#dfe3e8",
    strokeWidth: 1,
    cornerRadius: 3.5,
    shadowColor: "rgba(15, 23, 42, 0.18)",
    shadowBlur: 36,
    shadowOffsetY: 16,
    effect: "glass",
    effectStrength: 0.58,
    zIndex: -70,
  }, 0.04);
  const graphics: GeneratedMotionGraphic[] = [background, topGlow, panel];

  graphics.push(
    withAxisReveal(graphic("feature-reveal", "header-rule", chartStart, chartEnd, { x: 50, y: 34, scale: 1, rotation: 0, opacity: 0.16 }, { width: 78, height: 0.3, fill: palette.foreground, zIndex: -40 }), "x", 0.06),
    withAxisReveal(graphic("feature-reveal", "baseline", chartStart, chartEnd, { x: 50, y: 74, scale: 1, rotation: 0, opacity: 0.22 }, { width: 78, height: 0.35, fill: palette.foreground, zIndex: -40 }), "x", 0.1),
  );
  [20, 34, 48, 62, 76, 90].forEach((x, index) => {
    graphics.push(withAxisReveal(graphic("feature-reveal", `tick-${index + 1}`, chartStart, chartEnd, { x, y: 54, scale: 1, rotation: 0, opacity: index === 0 || index === 5 ? 0.16 : 0.09 }, {
      width: 0.16,
      height: 40,
      fill: palette.foreground,
      zIndex: -35,
    }), "y", 0.08 + index * 0.025));
  });

  const bars = [
    graphic("feature-reveal", "bar-discover", phaseTime(duration, 0.16), revealEnd, { x: 27, y: 43, scale: 1, rotation: 0, opacity: 1 }, {
      width: 24,
      height: 7.5,
      fill: palette.accent,
      fillType: "linear",
      fillSecondary: "#76bcff",
      gradientAngle: 10,
      cornerRadius: 12,
      shadowColor: "rgba(70, 136, 220, 0.28)",
      shadowBlur: 18,
      shadowOffsetY: 8,
      effect: "glow",
      effectStrength: 0.2,
      zIndex: 10,
    }),
    graphic("feature-reveal", "bar-compose", phaseTime(duration, 0.22), revealEnd, { x: 58, y: 43, scale: 1, rotation: 0, opacity: 1 }, {
      width: 26,
      height: 7.5,
      fill: "#a882dd",
      fillType: "linear",
      fillSecondary: "#cfb4f2",
      gradientAngle: 8,
      cornerRadius: 12,
      shadowColor: "rgba(113, 78, 168, 0.22)",
      shadowBlur: 16,
      shadowOffsetY: 7,
      effect: "glow",
      effectStrength: 0.16,
      zIndex: 11,
    }),
    graphic("feature-reveal", "bar-automate", phaseTime(duration, 0.28), revealEnd, { x: 52, y: 55, scale: 1, rotation: 0, opacity: 1 }, {
      width: 64,
      height: 7.5,
      fill: palette.accent2,
      fillType: "linear",
      fillSecondary: "#f0a27c",
      gradientAngle: 4,
      cornerRadius: 12,
      shadowColor: "rgba(185, 91, 57, 0.22)",
      shadowBlur: 18,
      shadowOffsetY: 8,
      effect: "chromatic",
      effectStrength: 0.13,
      zIndex: 12,
    }),
    graphic("feature-reveal", "bar-publish", phaseTime(duration, 0.34), revealEnd, { x: 46, y: 67, scale: 1, rotation: 0, opacity: 1 }, {
      width: 40,
      height: 7.5,
      fill: "#efcf6a",
      fillType: "linear",
      fillSecondary: "#f8e6a6",
      gradientAngle: 6,
      cornerRadius: 12,
      shadowColor: "rgba(159, 122, 22, 0.18)",
      shadowBlur: 15,
      shadowOffsetY: 7,
      effect: "glow",
      effectStrength: 0.13,
      zIndex: 13,
    }),
  ];
  bars.forEach((bar, index) => graphics.push(withAxisReveal(bar, "x", 0.04 + index * 0.05)));

  const playhead = graphic("feature-reveal", "playhead", phaseTime(duration, 0.38), revealEnd, { x: 78, y: 54, scale: 1, rotation: 0, opacity: 0.72 }, {
    kind: "line",
    width: 0.22,
    height: 44,
    fill: "#1f2937",
    effect: "glow",
    effectStrength: 0.24,
    zIndex: 30,
  });
  const playheadDuration = Math.max(0.05, playhead.end - playhead.start);
  playhead.keyframes = [
    motionFrame(`${playhead.id}-start`, 0, { x: 19, y: 54, scale: 1, rotation: 0, opacity: 0 }, "hold"),
    motionFrame(`${playhead.id}-arrive`, playheadDuration * 0.12, { x: 19, y: 54, scale: 1, rotation: 0, opacity: 0.72 }, "snappy"),
    motionFrame(`${playhead.id}-scrub`, playheadDuration * 0.78, { x: 82, y: 54, scale: 1, rotation: 0, opacity: 0.72 }, "gentle", "smooth"),
    motionFrame(`${playhead.id}-out`, playheadDuration, { x: 84, y: 54, scale: 1, rotation: 0, opacity: 0 }, "easeIn"),
  ];
  const playheadCap = graphic("feature-reveal", "playhead-cap", phaseTime(duration, 0.38), revealEnd, { x: 78, y: 31.5, scale: 1, rotation: 0, opacity: 0.9 }, {
    kind: "path",
    width: 2.2,
    height: 3.8,
    fill: palette.foreground,
    pathData: "M 6 8 L 94 8 L 50 92 Z",
    zIndex: 31,
  });
  playheadCap.keyframes = playhead.keyframes.map((frame, index) => ({
    ...frame,
    id: `${playheadCap.id}-${index}`,
    y: 31.5,
    opacity: Math.min(0.9, frame.opacity * 1.25),
  }));
  graphics.push(
    playhead,
    playheadCap,
    graphic("feature-reveal", "status-dot", phaseTime(duration, 0.44), revealEnd, { x: 12.5, y: 67, scale: 1, rotation: 0, opacity: 1 }, { kind: "ellipse", width: 1.7, height: 3, fill: "#61b98e", fillSecondary: "#61b98e", shadowColor: "rgba(50, 145, 100, 0.35)", shadowBlur: 12, effect: "glow", effectStrength: 0.62, zIndex: 20 }, 0.08),
    graphic("feature-reveal", "outro", phaseTime(duration, 0.9), duration, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 1 }, { width: 100, height: 100, fill: palette.foreground, fillType: "linear", fillSecondary: "#0f172a", gradientAngle: 145, effect: "vignette", effectStrength: 0.58, zIndex: 500 }),
  );
  const sweep = graphic("feature-reveal", "surface-sheen", phaseTime(duration, 0.46), phaseTime(duration, 0.76), { x: 20, y: 56, scale: 1, rotation: 12, opacity: 0 }, {
    width: 9,
    height: 64,
    fill: "rgba(255,255,255,0)",
    fillType: "linear",
    fillSecondary: "rgba(255,255,255,0.72)",
    gradientAngle: 0,
    blur: 10,
    blendMode: "screen",
    zIndex: 42,
  });
  const sweepDuration = Math.max(0.1, sweep.end - sweep.start);
  sweep.keyframes = [
    motionFrame(`${sweep.id}-start`, 0, { x: 16, y: 56, scale: 1, scaleX: 0.35, scaleY: 1, rotation: 12, opacity: 0 }, "hold"),
    motionFrame(`${sweep.id}-light`, sweepDuration * 0.18, { x: 28, y: 56, scale: 1, scaleX: 1, scaleY: 1, rotation: 12, opacity: 0.28 }, "easeOut"),
    motionFrame(`${sweep.id}-travel`, sweepDuration * 0.82, { x: 78, y: 56, scale: 1, scaleX: 1, scaleY: 1, rotation: 12, opacity: 0.22 }, "linear", "smooth"),
    motionFrame(`${sweep.id}-out`, sweepDuration, { x: 91, y: 56, scale: 1, scaleX: 0.4, scaleY: 1, rotation: 12, opacity: 0 }, "easeIn"),
  ];
  graphics.push(sweep, ...particleBurst("feature-reveal", duration, 0.35, 0.54, { x: 91, y: 9 }, [palette.accent, palette.accent2, "#efcf6a"], 8, 140));

  const labelStyle: Partial<GeneratedMotionCaption> = {
    fontFamily: "mono",
    fontWeight: 700,
    letterSpacing: 1.8,
    textTransform: "uppercase",
    size: 14,
    maxWidth: 24,
    reveal: "wipe",
    revealDuration: 0.32,
    zIndex: 110,
  };
  const captions: GeneratedMotionCaption[] = [
    caption("feature-reveal", "kicker", copy.kicker, phaseTime(duration, 0.02), revealEnd, { x: 6, y: 8, scale: 1, rotation: 0, opacity: 1 }, { size: 17, color: palette.accent2, align: "left", fontFamily: "mono", fontWeight: 700, letterSpacing: 2.6, textTransform: "uppercase", maxWidth: 45, reveal: "characters", revealDuration: 0.65 }),
    caption("feature-reveal", "headline", copy.headline, phaseTime(duration, 0.035), phaseTime(duration, 0.42), { x: 6, y: 20, scale: 1, rotation: 0, opacity: 1 }, { size: 62, color: palette.foreground, align: "left", fontFamily: "display", fontWeight: 800, letterSpacing: -1.8, lineHeight: 0.94, maxWidth: 72, reveal: "words", revealDuration: 0.55, textShadowColor: "rgba(15, 23, 42, 0.12)", textShadowBlur: 16 }, 0.04),
    caption("feature-reveal", "timecode", "00:00 — 00:15", chartStart, chartEnd, { x: 91, y: 29, scale: 1, rotation: 0, opacity: 0.55 }, { size: 13, color: palette.foreground, align: "right", fontFamily: "mono", fontWeight: 500, letterSpacing: 1.2, maxWidth: 30 }, 0.1),
    caption("feature-reveal", "discover", "Discover", phaseTime(duration, 0.16), revealEnd, { x: 27, y: 43, scale: 1, rotation: 0, opacity: 1 }, { ...labelStyle, color: contrastTextColor(palette.accent), align: "center" }),
    caption("feature-reveal", "compose", "Compose", phaseTime(duration, 0.22), revealEnd, { x: 58, y: 43, scale: 1, rotation: 0, opacity: 1 }, { ...labelStyle, color: "#241b33", align: "center" }),
    caption("feature-reveal", "automate", "Automate", phaseTime(duration, 0.28), revealEnd, { x: 52, y: 55, scale: 1, rotation: 0, opacity: 1 }, { ...labelStyle, color: contrastTextColor(palette.accent2), align: "center", maxWidth: 58 }),
    caption("feature-reveal", "publish", "Publish", phaseTime(duration, 0.34), revealEnd, { x: 46, y: 67, scale: 1, rotation: 0, opacity: 1 }, { ...labelStyle, color: "#32260b", align: "center", maxWidth: 36 }),
    caption("feature-reveal", "status", "READY", phaseTime(duration, 0.44), revealEnd, { x: 14.5, y: 67, scale: 1, rotation: 0, opacity: 0.66 }, { size: 11, color: palette.foreground, align: "left", fontFamily: "mono", fontWeight: 700, letterSpacing: 1.7, maxWidth: 18 }, 0.08),
    caption("feature-reveal", "stat", copy.statValue, phaseTime(duration, 0.34), revealEnd, { x: 91, y: 9, scale: 1, rotation: 0, opacity: 1 }, { size: 38, color: palette.foreground, align: "right", fontFamily: "display", fontWeight: 800, letterSpacing: -1.4, maxWidth: 14, reveal: "characters", revealDuration: 0.28 }, 0.05),
    caption("feature-reveal", "stat-label", copy.statLabel, phaseTime(duration, 0.36), revealEnd, { x: 91, y: 15, scale: 1, rotation: 0, opacity: 0.58 }, { size: 10, color: palette.foreground, align: "right", fontFamily: "mono", fontWeight: 700, letterSpacing: 1.2, textTransform: "uppercase", maxWidth: 24, reveal: "wipe", revealDuration: 0.32 }, 0.08),
    caption("feature-reveal", "subhead", copy.subhead, phaseTime(duration, 0.5), revealEnd, { x: 91, y: 84, scale: 1, rotation: 0, opacity: 1 }, { size: 21, color: palette.foreground, align: "right", fontFamily: "sans", fontWeight: 500, lineHeight: 1.25, maxWidth: 55, reveal: "words", revealDuration: 0.72 }, 0.08),
    caption("feature-reveal", "cta", copy.cta, phaseTime(duration, 0.9), duration, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 1 }, { size: 54, color: palette.background, align: "center", fontFamily: "display", fontWeight: 800, letterSpacing: -1.2, maxWidth: 72, reveal: "wipe", revealDuration: 0.35, zIndex: 600 }),
  ];
  return { captions, graphics };
}

function buildProductStory(
  duration: number,
  palette: MotionDesignPalette,
  copy: typeof DEFAULT_COPY[MotionDesignPreset],
): Pick<GeneratedMotionDesign, "captions" | "graphics"> {
  const storyStart = phaseTime(duration, 0.18);
  const storyEnd = phaseTime(duration, 0.86);
  const surfaceText = contrastTextColor(palette.surface);
  const graphics: GeneratedMotionGraphic[] = [
    graphic("product-story", "background", 0, duration, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 1 }, {
      width: 100,
      height: 100,
      fill: palette.background,
      fillType: "linear",
      fillSecondary: "#e9e5dc",
      gradientAngle: 118,
      effect: "grain",
      effectStrength: 0.3,
      zIndex: -100,
    }),
    withCameraDrift(graphic("product-story", "paper-glow", 0, storyEnd, { x: 78, y: 28, scale: 1, rotation: 0, opacity: 0.42 }, {
      kind: "ellipse",
      width: 50,
      height: 74,
      fill: "#ffffff",
      fillType: "radial",
      fillSecondary: "rgba(255, 255, 255, 0)",
      blur: 30,
      blendMode: "screen",
      effect: "glow",
      effectStrength: 0.32,
      zIndex: -90,
    }), -3, 2, 1.12),
    graphic("product-story", "back-window", storyStart, storyEnd, { x: 46, y: 51, scale: 1, rotation: -6, opacity: 0.72 }, {
      width: 74,
      height: 57,
      fill: "#101215",
      fillType: "linear",
      fillSecondary: "#1f2228",
      gradientAngle: 150,
      stroke: "rgba(255, 255, 255, 0.08)",
      strokeWidth: 0.5,
      cornerRadius: 4.5,
      shadowColor: "rgba(17, 24, 39, 0.26)",
      shadowBlur: 26,
      shadowOffsetY: 15,
      zIndex: 0,
    }, 0.02),
    graphic("product-story", "middle-window", phaseTime(duration, 0.23), storyEnd, { x: 50, y: 52, scale: 1, rotation: -3, opacity: 0.88 }, {
      width: 75,
      height: 58,
      fill: "#131519",
      fillType: "linear",
      fillSecondary: "#23262d",
      gradientAngle: 140,
      stroke: "rgba(255, 255, 255, 0.09)",
      strokeWidth: 0.5,
      cornerRadius: 4.5,
      shadowColor: "rgba(17, 24, 39, 0.34)",
      shadowBlur: 30,
      shadowOffsetY: 16,
      zIndex: 10,
    }, 0.06),
    graphic("product-story", "front-window", phaseTime(duration, 0.29), storyEnd, { x: 54, y: 53, scale: 1, rotation: 0, opacity: 1 }, {
      width: 76,
      height: 59,
      fill: palette.surface,
      fillType: "linear",
      fillSecondary: "#20242a",
      gradientAngle: 150,
      stroke: "rgba(255, 255, 255, 0.12)",
      strokeWidth: 0.55,
      cornerRadius: 4.5,
      shadowColor: "rgba(17, 24, 39, 0.4)",
      shadowBlur: 36,
      shadowOffsetY: 18,
      effect: "scanlines",
      effectStrength: 0.15,
      zIndex: 20,
    }, 0.1),
    graphic("product-story", "browser-bar", phaseTime(duration, 0.31), storyEnd, { x: 54, y: 27.2, scale: 1, rotation: 0, opacity: 0.96 }, { width: 76, height: 7.5, fill: "rgba(15,17,20,0.88)", cornerRadius: 4.5, effect: "glass", effectStrength: 0.42, zIndex: 22 }, 0.12),
    graphic("product-story", "dot-red", phaseTime(duration, 0.32), storyEnd, { x: 18.5, y: 27.1, scale: 1, rotation: 0, opacity: 0.9 }, { kind: "ellipse", width: 0.85, height: 1.5, fill: "#ef735f", zIndex: 24 }, 0.12),
    graphic("product-story", "dot-yellow", phaseTime(duration, 0.33), storyEnd, { x: 20.2, y: 27.1, scale: 1, rotation: 0, opacity: 0.9 }, { kind: "ellipse", width: 0.85, height: 1.5, fill: "#e6bd58", zIndex: 24 }, 0.14),
    graphic("product-story", "dot-green", phaseTime(duration, 0.34), storyEnd, { x: 21.9, y: 27.1, scale: 1, rotation: 0, opacity: 0.9 }, { kind: "ellipse", width: 0.85, height: 1.5, fill: "#67c38d", zIndex: 24 }, 0.16),
    graphic("product-story", "address", phaseTime(duration, 0.34), storyEnd, { x: 54, y: 27.2, scale: 1, rotation: 0, opacity: 0.72 }, { width: 38, height: 2.6, fill: "rgba(255, 255, 255, 0.08)", cornerRadius: 12, zIndex: 24 }, 0.16),
    graphic("product-story", "sidebar", phaseTime(duration, 0.35), storyEnd, { x: 21.7, y: 55, scale: 1, rotation: 0, opacity: 0.82 }, { width: 11.5, height: 47, fill: "#111318", cornerRadius: 3, zIndex: 23 }, 0.16),
    graphic("product-story", "hero-card", phaseTime(duration, 0.38), storyEnd, { x: 48, y: 46.5, scale: 1, rotation: 0, opacity: 1 }, {
      width: 37,
      height: 19,
      fill: "#0f1115",
      fillType: "linear",
      fillSecondary: "#292d35",
      gradientAngle: 125,
      stroke: "rgba(255, 255, 255, 0.08)",
      strokeWidth: 0.45,
      cornerRadius: 5,
      shadowColor: "rgba(0, 0, 0, 0.34)",
      shadowBlur: 16,
      shadowOffsetY: 7,
      effect: "glass",
      effectStrength: 0.38,
      zIndex: 28,
    }, 0.18),
    graphic("product-story", "action-card", phaseTime(duration, 0.43), storyEnd, { x: 73.5, y: 50, scale: 1, rotation: 0, opacity: 1 }, {
      width: 12,
      height: 26,
      fill: palette.accent,
      fillType: "linear",
      fillSecondary: "#ffb06f",
      gradientAngle: 145,
      cornerRadius: 5,
      shadowColor: "rgba(240, 138, 80, 0.32)",
      shadowBlur: 18,
      shadowOffsetY: 8,
      effect: "glow",
      effectStrength: 0.34,
      zIndex: 29,
    }, 0.21),
    withAxisReveal(graphic("product-story", "input", phaseTime(duration, 0.46), storyEnd, { x: 48, y: 64, scale: 1, rotation: 0, opacity: 1 }, { width: 37, height: 5.2, fill: "rgba(255, 255, 255, 0.08)", stroke: "rgba(255, 255, 255, 0.12)", strokeWidth: 0.45, cornerRadius: 12, effect: "glass", effectStrength: 0.3, zIndex: 28 }), "x", 0.12),
    graphic("product-story", "status-pill", phaseTime(duration, 0.5), storyEnd, { x: 48, y: 72, scale: 1, rotation: 0, opacity: 1 }, { width: 23, height: 5.4, fill: palette.accent2, fillType: "linear", fillSecondary: "#a9d6d7", gradientAngle: 5, cornerRadius: 18, zIndex: 29 }, 0.24),
    graphic("product-story", "outro", phaseTime(duration, 0.84), duration, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 1 }, {
      width: 100,
      height: 100,
      fill: palette.surface,
      fillType: "linear",
      fillSecondary: "#0c0e11",
      gradientAngle: 145,
      effect: "vignette",
      effectStrength: 0.62,
      zIndex: 500,
    }),
  ];
  const cursor = graphic("product-story", "cursor", phaseTime(duration, 0.52), storyEnd, { x: 68, y: 67, scale: 1, rotation: -12, opacity: 1 }, {
    kind: "path",
    width: 2.3,
    height: 4.6,
    fill: "#ffffff",
    stroke: "#111318",
    strokeWidth: 0.6,
    pathData: "M 8 5 L 90 49 L 57 59 L 43 95 Z",
    shadowColor: "rgba(0, 0, 0, 0.45)",
    shadowBlur: 8,
    effect: "chromatic",
    effectStrength: 0.3,
    zIndex: 40,
  });
  const cursorDuration = Math.max(0.05, cursor.end - cursor.start);
  cursor.keyframes = [
    motionFrame(`${cursor.id}-start`, 0, { x: 61, y: 59, scale: 0.9, rotation: -18, opacity: 0 }, "hold"),
    motionFrame(`${cursor.id}-arrive`, cursorDuration * 0.2, { x: 68, y: 67, scale: 1, rotation: -12, opacity: 1 }, "snappy", "smooth"),
    motionFrame(`${cursor.id}-click`, cursorDuration * 0.5, { x: 73.5, y: 50, scale: 0.82, rotation: -8, opacity: 1 }, "easeInOut", "smooth"),
    motionFrame(`${cursor.id}-confirm`, cursorDuration * 0.7, { x: 48, y: 72, scale: 1, rotation: -14, opacity: 1 }, "gentle", "smooth"),
    motionFrame(`${cursor.id}-out`, cursorDuration, { x: 45, y: 75, scale: 1, rotation: -14, opacity: 0 }, "easeIn"),
  ];
  graphics.push(
    cursor,
    clickRipple("product-story", duration, 0.62, { x: 73.5, y: 50 }, palette.accent, 0),
    clickRipple("product-story", duration, 0.62, { x: 73.5, y: 50 }, palette.accent2, 1),
  );
  const captions: GeneratedMotionCaption[] = [
    caption("product-story", "kicker", copy.kicker, phaseTime(duration, 0.02), storyEnd, { x: 6, y: 8, scale: 1, rotation: 0, opacity: 1 }, { size: 14, color: palette.accent, align: "left", fontFamily: "mono", fontWeight: 700, letterSpacing: 2.4, textTransform: "uppercase", maxWidth: 40, reveal: "characters", revealDuration: 0.52, zIndex: 80 }),
    caption("product-story", "headline", copy.headline, phaseTime(duration, 0.04), phaseTime(duration, 0.3), { x: 6, y: 21, scale: 1, rotation: 0, opacity: 1 }, { size: 58, color: palette.foreground, align: "left", fontFamily: "display", fontWeight: 800, letterSpacing: -1.6, lineHeight: 0.94, maxWidth: 70, reveal: "words", revealDuration: 0.52, zIndex: 80 }, 0.04),
    caption("product-story", "browser-label", "MANOR / STORYBOARD", phaseTime(duration, 0.34), storyEnd, { x: 34, y: 27.2, scale: 1, rotation: 0, opacity: 0.55 }, { size: 9, color: surfaceText, align: "left", fontFamily: "mono", fontWeight: 600, letterSpacing: 1, maxWidth: 30, zIndex: 35 }, 0.1),
    caption("product-story", "window-title", copy.subhead, phaseTime(duration, 0.36), storyEnd, { x: 31.5, y: 42, scale: 1, rotation: 0, opacity: 1 }, { size: 23, color: surfaceText, align: "left", fontFamily: "sans", fontWeight: 650, lineHeight: 1.12, maxWidth: 34, reveal: "words", revealDuration: 0.58, zIndex: 36 }, 0.12),
    caption("product-story", "stat", copy.statValue, phaseTime(duration, 0.46), storyEnd, { x: 73.5, y: 49, scale: 1, rotation: 0, opacity: 1 }, { size: 38, color: contrastTextColor(palette.accent), align: "center", fontFamily: "display", fontWeight: 800, letterSpacing: -1, maxWidth: 10, zIndex: 36 }, 0.16),
    caption("product-story", "stat-label", copy.statLabel, phaseTime(duration, 0.5), storyEnd, { x: 48, y: 72, scale: 1, rotation: 0, opacity: 1 }, { size: 10, color: contrastTextColor(palette.accent2), align: "center", fontFamily: "mono", fontWeight: 700, letterSpacing: 1.1, textTransform: "uppercase", maxWidth: 20, reveal: "wipe", revealDuration: 0.32, zIndex: 36 }, 0.18),
    caption("product-story", "cta", copy.cta, phaseTime(duration, 0.86), duration, { x: 50, y: 52, scale: 1, rotation: 0, opacity: 1 }, { style: "lowerThird", size: 50, color: surfaceText, backgroundColor: palette.accent, background: palette.accent, backgroundOpacity: 0.12, align: "center", fontFamily: "display", fontWeight: 800, letterSpacing: -1.2, maxWidth: 68, reveal: "wipe", revealDuration: 0.34, zIndex: 600 }),
  ];
  return { captions, graphics };
}

function buildMotionDesign(
  duration: number,
  palette: MotionDesignPalette,
  copy: typeof DEFAULT_COPY[MotionDesignPreset],
): Pick<GeneratedMotionDesign, "captions" | "graphics"> {
  const hookEnd = phaseTime(duration, 0.28);
  const buildStart = phaseTime(duration, 0.2);
  const buildEnd = phaseTime(duration, 0.66);
  const proofStart = phaseTime(duration, 0.56);
  const proofEnd = phaseTime(duration, 0.86);
  const resolveStart = phaseTime(duration, 0.82);
  const matteTop = graphic("motion-design", "cinema-matte-top", 0, duration, { x: 50, y: 1.35, scale: 1, rotation: 0, opacity: 1 }, {
    width: 100,
    height: 2.7,
    fill: "#050507",
    zIndex: 980,
  });
  matteTop.keyframes = [
    motionFrame(`${matteTop.id}-open`, 0, { x: 50, y: -1.4, scale: 1, rotation: 0, opacity: 1 }, "hold"),
    motionFrame(`${matteTop.id}-settle`, Math.min(0.28, duration * 0.06), { x: 50, y: 1.35, scale: 1, rotation: 0, opacity: 1 }, "easeOut"),
    motionFrame(`${matteTop.id}-hold`, duration, { x: 50, y: 1.35, scale: 1, rotation: 0, opacity: 1 }, "linear"),
  ];
  const matteBottom = graphic("motion-design", "cinema-matte-bottom", 0, duration, { x: 50, y: 98.65, scale: 1, rotation: 0, opacity: 1 }, {
    width: 100,
    height: 2.7,
    fill: "#050507",
    zIndex: 980,
  });
  matteBottom.keyframes = [
    motionFrame(`${matteBottom.id}-open`, 0, { x: 50, y: 101.4, scale: 1, rotation: 0, opacity: 1 }, "hold"),
    motionFrame(`${matteBottom.id}-settle`, Math.min(0.28, duration * 0.06), { x: 50, y: 98.65, scale: 1, rotation: 0, opacity: 1 }, "easeOut"),
    motionFrame(`${matteBottom.id}-hold`, duration, { x: 50, y: 98.65, scale: 1, rotation: 0, opacity: 1 }, "linear"),
  ];
  const opticalVignette = graphic("motion-design", "optical-vignette", 0, duration, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 0.7 }, {
    width: 100,
    height: 100,
    fill: "rgba(0,0,0,0)",
    effect: "vignette",
    effectStrength: 0.5,
    zIndex: 940,
  });
  opticalVignette.keyframes = [];
  const filmGrain = graphic("motion-design", "film-grain-overlay", 0, duration, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 0.3 }, {
    width: 100,
    height: 100,
    fill: "rgba(0,0,0,0)",
    blendMode: "soft-light",
    effect: "grain",
    effectStrength: 0.34,
    zIndex: 950,
  });
  filmGrain.keyframes = [];
  const graphics: GeneratedMotionGraphic[] = [
    graphic("motion-design", "background", 0, duration, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 1 }, {
      width: 100,
      height: 100,
      fill: palette.background,
      fillType: "radial",
      fillSecondary: "#1a1715",
      effect: "grain",
      effectStrength: 0.18,
      zIndex: -100,
    }),
    withCameraDrift(graphic("motion-design", "ambient-warm", 0, duration, { x: 18, y: 38, scale: 1, rotation: 0, opacity: 0.18 }, {
      kind: "ellipse",
      width: 42,
      height: 74,
      fill: palette.accent,
      fillType: "radial",
      fillSecondary: "rgba(255, 106, 61, 0)",
      blur: 26,
      blendMode: "screen",
      effect: "glow",
      effectStrength: 0.22,
      zIndex: -80,
    }), 2.2, 1.1, 1.06),
    withCameraDrift(graphic("motion-design", "ambient-gold", phaseTime(duration, 0.02), duration, { x: 82, y: 68, scale: 1, rotation: 0, opacity: 0.12 }, {
      kind: "ellipse",
      width: 32,
      height: 56,
      fill: palette.accent2,
      fillType: "radial",
      fillSecondary: "rgba(244, 201, 93, 0)",
      blur: 24,
      blendMode: "screen",
      effect: "glow",
      effectStrength: 0.18,
      zIndex: -75,
    }), -1.6, -1.2, 1.05),
    graphic("motion-design", "frame-outline", phaseTime(duration, 0.01), resolveStart, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 0.24 }, {
      width: 92,
      height: 84,
      fill: "rgba(0,0,0,0)",
      stroke: "rgba(245, 239, 229, 0.34)",
      strokeWidth: 0.5,
      cornerRadius: 2,
      zIndex: -20,
    }),
    matteTop,
    matteBottom,
    opticalVignette,
    filmGrain,
    opticalTransition("motion-design", duration, "hook-to-editor-light-leak", 0.17, 0.285, "lightLeak", -18, 84, 0.86, 0.78, 900),
    opticalTransition("motion-design", duration, "editor-anamorphic-flare", 0.38, 0.54, "anamorphic", 12, 84, 0.42, 0.58, 360),
    opticalTransition("motion-design", duration, "editor-rack-focus", 0.525, 0.625, "halation", 42, 58, 0.62, 0.7, 910),
    opticalTransition("motion-design", duration, "proof-to-resolve-film-burn", 0.79, 0.885, "filmBurn", -22, 72, 0.92, 0.84, 920),
    graphic("motion-design", "deterministic-particle-atmosphere", buildStart, resolveStart, { x: 50, y: 52, scale: 1, rotation: 0, opacity: 0.82 }, {
      kind: "particle",
      width: 88,
      height: 76,
      fill: palette.accent2,
      fillSecondary: palette.accent,
      stroke: palette.foreground,
      particleCount: 22,
      particleShape: "spark",
      particleMotion: "drift",
      particleSeed: 2046,
      particleSize: 0.62,
      particleSpeed: 0.74,
      particleGravity: -0.08,
      particleSpread: 0.92,
      particleLoop: true,
      blendMode: "screen",
      zIndex: 12,
    }),

    /* Hook world: oversized editorial type, edge anchors, and one branded mark. */
    withAxisReveal(graphic("motion-design", "hook-rule", phaseTime(duration, 0.02), hookEnd, { x: 28, y: 18, scale: 1, rotation: 0, opacity: 1 }, {
      width: 40,
      height: 0.55,
      fill: palette.accent,
      zIndex: 18,
    }), "x", 0.03),
    graphic("motion-design", "top-block", phaseTime(duration, 0.05), hookEnd, { x: 84, y: 23, scale: 1, rotation: 0, opacity: 1 }, {
      width: 7.2,
      height: 10.5,
      fill: palette.accent,
      fillType: "linear",
      fillSecondary: palette.accent2,
      gradientAngle: 42,
      maskShape: "hexagon",
      effect: "glow",
      effectStrength: 0.36,
      shadowColor: "rgba(255, 106, 61, 0.35)",
      shadowBlur: 24,
      zIndex: 30,
    }, 0.06),
    graphic("motion-design", "hook-register", phaseTime(duration, 0.08), hookEnd, { x: 88, y: 76, scale: 1, rotation: 0, opacity: 0.58 }, {
      kind: "path",
      width: 8,
      height: 14,
      fill: "rgba(0,0,0,0)",
      stroke: palette.accent2,
      strokeWidth: 1.3,
      pathData: "M 50 0 L 50 100 M 0 50 L 100 50 M 18 18 L 82 82 M 82 18 L 18 82",
      zIndex: 22,
    }, 0.08),

    /* Build world: a real editor UI assembles in depth instead of abstract decoration. */
    withCameraDrift(graphic("motion-design", "foreground-bokeh-left", buildStart, buildEnd, { x: 4, y: 68, scale: 1, rotation: 0, opacity: 0.12 }, {
      kind: "ellipse",
      width: 9,
      height: 16,
      fill: palette.accent,
      fillType: "radial",
      fillSecondary: "rgba(255,106,61,0)",
      blur: 24,
      blendMode: "screen",
      effect: "halation",
      effectStrength: 0.42,
      zIndex: 320,
    }), 3.5, -2.5, 1.18),
    withCameraDrift(graphic("motion-design", "foreground-bokeh-right", phaseTime(duration, 0.22), buildEnd, { x: 96, y: 31, scale: 1, rotation: 0, opacity: 0.1 }, {
      kind: "ellipse",
      width: 7,
      height: 12.5,
      fill: palette.accent2,
      fillType: "radial",
      fillSecondary: "rgba(244,201,93,0)",
      blur: 22,
      blendMode: "screen",
      effect: "halation",
      effectStrength: 0.36,
      zIndex: 320,
    }), -2.8, 2.2, 1.14),
    graphic("motion-design", "editor-depth-back", phaseTime(duration, 0.195), buildEnd, { x: 54, y: 51, scale: 0.97, rotation: 3.4, opacity: 0.32 }, {
      width: 78,
      height: 64,
      fill: "#111319",
      stroke: "rgba(244, 201, 93, 0.28)",
      strokeWidth: 0.7,
      cornerRadius: 5,
      blur: 1.5,
      shadowColor: "rgba(0,0,0,0.36)",
      shadowBlur: 24,
      shadowOffsetY: 12,
      zIndex: 4,
    }, 0.02),
    graphic("motion-design", "editor-depth-mid", phaseTime(duration, 0.2), buildEnd, { x: 52, y: 51.5, scale: 0.985, rotation: 1.1, opacity: 0.52 }, {
      width: 80,
      height: 66,
      fill: "#181a21",
      stroke: "rgba(245, 239, 229, 0.24)",
      strokeWidth: 0.6,
      cornerRadius: 5,
      shadowColor: "rgba(0,0,0,0.38)",
      shadowBlur: 20,
      shadowOffsetY: 10,
      zIndex: 6,
    }, 0.04),
    graphic("motion-design", "editor-shadow", buildStart, buildEnd, { x: 52, y: 53, scale: 1, rotation: -1.2, opacity: 0.5 }, {
      width: 82,
      height: 68,
      fill: "rgba(0,0,0,0.38)",
      blur: 18,
      cornerRadius: 5,
      zIndex: 8,
    }),
    graphic("motion-design", "editor-shell", buildStart, buildEnd, { x: 50, y: 50, scale: 1, rotation: -1.2, opacity: 1 }, {
      width: 82,
      height: 68,
      fill: palette.surface,
      fillType: "linear",
      fillSecondary: "#242018",
      gradientAngle: 145,
      stroke: "rgba(245, 239, 229, 0.42)",
      strokeWidth: 0.8,
      cornerRadius: 5,
      effect: "glass",
      effectStrength: 0.32,
      shadowColor: "rgba(0,0,0,0.48)",
      shadowBlur: 34,
      shadowOffsetY: 18,
      zIndex: 12,
    }),
    graphic("motion-design", "editor-topbar", phaseTime(duration, 0.21), buildEnd, { x: 50, y: 20.5, scale: 1, rotation: -1.2, opacity: 1 }, {
      width: 77,
      height: 7.5,
      fill: "#22242c",
      fillType: "linear",
      fillSecondary: "#15171d",
      gradientAngle: 0,
      effect: "scanlines",
      effectStrength: 0.18,
      cornerRadius: 24,
      zIndex: 16,
    }, 0.04),
    graphic("motion-design", "topbar-status-dot", phaseTime(duration, 0.225), buildEnd, { x: 14.7, y: 20.6, scale: 1, rotation: -1.2, opacity: 1 }, {
      kind: "ellipse",
      width: 1.15,
      height: 2.05,
      fill: palette.accent,
      effect: "glow",
      effectStrength: 0.32,
      zIndex: 20,
    }, 0.04),
    graphic("motion-design", "topbar-progress", phaseTime(duration, 0.235), buildEnd, { x: 68.5, y: 20.6, scale: 1, rotation: -1.2, opacity: 0.62 }, {
      width: 10,
      height: 0.7,
      fill: "#3f424b",
      cornerRadius: 30,
      zIndex: 20,
    }, 0.06),
    withAxisReveal(graphic("motion-design", "topbar-progress-live", phaseTime(duration, 0.245), buildEnd, { x: 65.8, y: 20.6, scale: 1, rotation: -1.2, opacity: 1 }, {
      width: 4.6,
      height: 0.7,
      fill: palette.accent2,
      cornerRadius: 30,
      effect: "glow",
      effectStrength: 0.2,
      zIndex: 21,
    }), "x", 0.06),
    graphic("motion-design", "editor-sidebar", phaseTime(duration, 0.23), buildEnd, { x: 16.5, y: 48.5, scale: 1, rotation: -1.2, opacity: 1 }, {
      width: 10.5,
      height: 47,
      fill: "#202229",
      fillType: "linear",
      fillSecondary: "#13151a",
      gradientAngle: 90,
      cornerRadius: 8,
      zIndex: 17,
    }, 0.05),
    graphic("motion-design", "editor-canvas", phaseTime(duration, 0.24), buildEnd, { x: 44, y: 44, scale: 1, rotation: -1.2, opacity: 1 }, {
      width: 43,
      height: 38,
      fill: "#0f1117",
      fillType: "radial",
      fillSecondary: "#291c17",
      stroke: "rgba(244, 201, 93, 0.24)",
      strokeWidth: 0.5,
      cornerRadius: 4,
      effect: "vignette",
      effectStrength: 0.55,
      zIndex: 18,
    }, 0.06),
    graphic("motion-design", "editor-inspector", phaseTime(duration, 0.26), buildEnd, { x: 75.5, y: 44, scale: 1, rotation: -1.2, opacity: 1 }, {
      width: 17,
      height: 38,
      fill: "#1c1e25",
      fillType: "linear",
      fillSecondary: "#13151a",
      gradientAngle: 90,
      stroke: "rgba(245,239,229,0.2)",
      strokeWidth: 0.5,
      cornerRadius: 5,
      zIndex: 18,
    }, 0.08),
    graphic("motion-design", "editor-timeline", phaseTime(duration, 0.28), buildEnd, { x: 50, y: 73.5, scale: 1, rotation: -1.2, opacity: 1 }, {
      width: 77,
      height: 14,
      fill: "#111319",
      stroke: "rgba(245,239,229,0.18)",
      strokeWidth: 0.5,
      cornerRadius: 8,
      zIndex: 18,
    }, 0.08),
    graphic("motion-design", "canvas-focus", phaseTime(duration, 0.3), buildEnd, { x: 44, y: 44, scale: 1, rotation: -1.2, opacity: 0.72 }, {
      kind: "ellipse",
      width: 22,
      height: 28,
      fill: palette.accent,
      fillType: "radial",
      fillSecondary: "rgba(255,106,61,0)",
      blur: 8,
      blendMode: "screen",
      effect: "glow",
      effectStrength: 0.28,
      zIndex: 19,
    }, 0.1),
    graphic("motion-design", "canvas-card", phaseTime(duration, 0.285), buildEnd, { x: 44, y: 44, scale: 1, rotation: -1.2, opacity: 0.9 }, {
      width: 25,
      height: 22,
      fill: "rgba(14, 15, 20, 0.72)",
      stroke: "rgba(245,239,229,0.24)",
      strokeWidth: 0.5,
      cornerRadius: 5,
      effect: "glass",
      effectStrength: 0.28,
      shadowColor: "rgba(0,0,0,0.35)",
      shadowBlur: 18,
      shadowOffsetY: 8,
      zIndex: 21,
    }, 0.08),
    withAxisReveal(graphic("motion-design", "canvas-code-line-a", phaseTime(duration, 0.315), buildEnd, { x: 44, y: 37.2, scale: 1, rotation: -1.2, opacity: 0.9 }, {
      width: 15.5,
      height: 0.9,
      fill: palette.accent,
      cornerRadius: 20,
      zIndex: 24,
    }), "x", 0.06),
    withAxisReveal(graphic("motion-design", "canvas-code-line-b", phaseTime(duration, 0.335), buildEnd, { x: 41.5, y: 40.2, scale: 1, rotation: -1.2, opacity: 0.62 }, {
      width: 10.5,
      height: 0.7,
      fill: palette.accent2,
      cornerRadius: 20,
      zIndex: 24,
    }), "x", 0.08),
    withAxisReveal(graphic("motion-design", "canvas-code-line-c", phaseTime(duration, 0.35), buildEnd, { x: 45.5, y: 48.7, scale: 1, rotation: -1.2, opacity: 0.42 }, {
      width: 13,
      height: 0.65,
      fill: palette.foreground,
      cornerRadius: 20,
      zIndex: 24,
    }), "x", 0.1),
    graphic("motion-design", "timeline-clip-a", phaseTime(duration, 0.31), buildEnd, { x: 36, y: 71.5, scale: 1, rotation: -1.2, opacity: 1 }, {
      width: 23,
      height: 3.4,
      fill: palette.accent,
      fillType: "linear",
      fillSecondary: "#ff9b74",
      gradientAngle: 0,
      cornerRadius: 30,
      effect: "glow",
      effectStrength: 0.18,
      zIndex: 23,
    }, 0.03),
    graphic("motion-design", "timeline-clip-b", phaseTime(duration, 0.33), buildEnd, { x: 53.5, y: 75.5, scale: 1, rotation: -1.2, opacity: 1 }, {
      width: 28,
      height: 3.3,
      fill: palette.accent2,
      fillType: "linear",
      fillSecondary: "#fff0a6",
      gradientAngle: 0,
      cornerRadius: 30,
      zIndex: 23,
    }, 0.05),
    graphic("motion-design", "timeline-clip-c", phaseTime(duration, 0.35), buildEnd, { x: 70, y: 70.5, scale: 1, rotation: -1.2, opacity: 0.78 }, {
      width: 14,
      height: 3.2,
      fill: "#777b87",
      cornerRadius: 30,
      zIndex: 23,
    }, 0.07),
    withAxisReveal(graphic("motion-design", "timeline-playhead", phaseTime(duration, 0.34), buildEnd, { x: 58, y: 73.2, scale: 1, rotation: -1.2, opacity: 1 }, {
      kind: "line",
      width: 0.14,
      height: 12,
      fill: palette.foreground,
      effect: "glow",
      effectStrength: 0.28,
      zIndex: 28,
    }), "y", 0.05),
    graphic("motion-design", "timeline-keyframe-a", phaseTime(duration, 0.37), buildEnd, { x: 42, y: 76.5, scale: 1, rotation: 45, opacity: 0.9 }, {
      width: 0.9,
      height: 1.6,
      fill: palette.foreground,
      maskShape: "diamond",
      zIndex: 27,
    }, 0.08),
    graphic("motion-design", "timeline-keyframe-b", phaseTime(duration, 0.39), buildEnd, { x: 61, y: 70.3, scale: 1, rotation: 45, opacity: 0.9 }, {
      width: 0.9,
      height: 1.6,
      fill: palette.accent2,
      maskShape: "diamond",
      effect: "glow",
      effectStrength: 0.2,
      zIndex: 27,
    }, 0.1),
  ];

  [29.5, 35, 40.5, 46].forEach((y, index) => {
    graphics.push(graphic("motion-design", `sidebar-row-${index + 1}`, phaseTime(duration, 0.25 + index * 0.015), buildEnd, {
      x: 16.5,
      y,
      scale: 1,
      rotation: -1.2,
      opacity: index === 1 ? 1 : 0.46,
    }, {
      width: index === 1 ? 6.8 : 5.2,
      height: 1.15,
      fill: index === 1 ? palette.accent : "#7b7e88",
      cornerRadius: 20,
      zIndex: 22,
    }, index * 0.035));
  });
  [31, 37, 43, 49].forEach((y, index) => {
    graphics.push(graphic("motion-design", `inspector-control-${index + 1}`, phaseTime(duration, 0.29 + index * 0.018), buildEnd, {
      x: 75.5,
      y,
      scale: 1,
      rotation: -1.2,
      opacity: 0.72,
    }, {
      width: index === 2 ? 11.5 : 12.5,
      height: index === 2 ? 3.4 : 2.2,
      fill: index === 2 ? palette.accent : "#30333c",
      stroke: index === 2 ? "rgba(255,255,255,0.25)" : "rgba(245,239,229,0.14)",
      strokeWidth: 0.4,
      cornerRadius: 28,
      effect: index === 2 ? "glow" : "none",
      effectStrength: 0.18,
      zIndex: 22,
    }, index * 0.03));
  });
  const cursor = graphic("motion-design", "editor-cursor", phaseTime(duration, 0.32), buildEnd, { x: 61, y: 57, scale: 1, rotation: -12, opacity: 1 }, {
    kind: "path",
    width: 3.4,
    height: 6.2,
    fill: palette.foreground,
    fillType: "solid",
    stroke: "rgba(0,0,0,0.55)",
    strokeWidth: 1,
    pathData: "M 7 5 L 91 56 L 56 65 L 39 96 Z",
    effect: "chromatic",
    effectStrength: 0.2,
    zIndex: 36,
  });
  const cursorDuration = Math.max(0.1, cursor.end - cursor.start);
  cursor.keyframes = [
    motionFrame(`${cursor.id}-in`, 0, { x: 61, y: 57, scale: 0.75, rotation: -12, opacity: 0 }, "hold"),
    motionFrame(`${cursor.id}-arrive`, cursorDuration * 0.22, { x: 68, y: 50, scale: 1, rotation: -12, opacity: 1 }, "snappy", "smooth"),
    motionFrame(`${cursor.id}-click`, cursorDuration * 0.46, { x: 75.5, y: 43, scale: 0.82, rotation: -12, opacity: 1 }, "easeInOut", "smooth"),
    motionFrame(`${cursor.id}-release`, cursorDuration * 0.6, { x: 75.5, y: 43, scale: 1, rotation: -12, opacity: 1 }, "spring"),
    motionFrame(`${cursor.id}-out`, cursorDuration, { x: 80, y: 38, scale: 0.9, rotation: -12, opacity: 0 }, "easeIn", "smooth"),
  ];
  graphics.push(
    cursor,
    clickRipple("motion-design", duration, 0.49, { x: 75.5, y: 43 }, palette.accent, 0, 34),
    clickRipple("motion-design", duration, 0.49, { x: 75.5, y: 43 }, palette.accent2, 1, 33),
  );

  /* Proof world: the number and its bars are one visual idea, not floating labels. */
  graphics.push(
    graphic("motion-design", "proof-surface", proofStart, proofEnd, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 1 }, {
      width: 90,
      height: 72,
      fill: "#f2eadf",
      fillType: "linear",
      fillSecondary: "#e4d7c7",
      gradientAngle: 128,
      cornerRadius: 3,
      shadowColor: "rgba(0,0,0,0.42)",
      shadowBlur: 34,
      shadowOffsetY: 16,
      effect: "grain",
      effectStrength: 0.14,
      zIndex: 120,
    }),
    graphic("motion-design", "proof-divider", phaseTime(duration, 0.59), proofEnd, { x: 49, y: 50, scale: 1, rotation: 0, opacity: 0.32 }, {
      kind: "line",
      width: 0.15,
      height: 56,
      fill: "#2b2925",
      zIndex: 126,
    }, 0.05),
    withAxisReveal(graphic("motion-design", "proof-baseline", phaseTime(duration, 0.6), proofEnd, { x: 72, y: 76, scale: 1, rotation: 0, opacity: 0.7 }, {
      width: 36,
      height: 0.32,
      fill: "#3a3530",
      zIndex: 128,
    }), "x", 0.04),
    withAxisReveal(graphic("motion-design", "proof-grid-top", phaseTime(duration, 0.59), proofEnd, { x: 75, y: 35, scale: 1, rotation: 0, opacity: 0.14 }, {
      width: 40,
      height: 0.18,
      fill: "#3a3530",
      zIndex: 126,
    }), "x", 0.05),
    withAxisReveal(graphic("motion-design", "proof-grid-mid", phaseTime(duration, 0.595), proofEnd, { x: 75, y: 54, scale: 1, rotation: 0, opacity: 0.12 }, {
      width: 40,
      height: 0.18,
      fill: "#3a3530",
      zIndex: 126,
    }), "x", 0.07),
    graphic("motion-design", "proof-trend", phaseTime(duration, 0.64), proofEnd, { x: 76, y: 49, scale: 1, rotation: 0, opacity: 0.72 }, {
      kind: "path",
      width: 39,
      height: 43,
      fill: "rgba(0,0,0,0)",
      stroke: palette.accent2,
      strokeWidth: 1.25,
      pathData: "M 2 84 C 14 82 18 70 29 68 S 44 55 55 58 S 69 38 79 41 S 91 17 98 12",
      effect: "glow",
      effectStrength: 0.22,
      zIndex: 136,
    }, 0.12),
  );
  const barHeights = [17, 27, 23, 39, 51];
  barHeights.forEach((height, index) => {
    const x = 59 + index * 7.1;
    graphics.push(withAxisReveal(graphic("motion-design", `proof-bar-${index + 1}`, phaseTime(duration, 0.61 + index * 0.018), proofEnd, {
      x,
      y: 76 - height / 2,
      scale: 1,
      rotation: 0,
      opacity: 1,
    }, {
      width: 4.5,
      height,
      fill: index === barHeights.length - 1 ? palette.accent : index === 3 ? palette.accent2 : "#4d4943",
      fillType: "linear",
      fillSecondary: index >= 3 ? "#ffb087" : "#777067",
      gradientAngle: 0,
      cornerRadius: 20,
      shadowColor: index >= 3 ? "rgba(255,106,61,0.3)" : "rgba(0,0,0,0.14)",
      shadowBlur: index >= 3 ? 16 : 5,
      effect: index === barHeights.length - 1 ? "glow" : "none",
      effectStrength: 0.24,
      zIndex: 130,
    }, index * 0.045), "y", index * 0.035));
  });

  /* Resolve world: a decisive brand-colored wipe and one strong CTA. */
  graphics.push(
    graphic("motion-design", "resolve-wipe", resolveStart, duration, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 1 }, {
      width: 100,
      height: 100,
      fill: palette.accent,
      fillType: "linear",
      fillSecondary: "#ff9a68",
      gradientAngle: 24,
      effect: "grain",
      effectStrength: 0.18,
      zIndex: 480,
    }),
    graphic("motion-design", "resolve-disc", phaseTime(duration, 0.85), duration, { x: 50, y: 49, scale: 1, rotation: 0, opacity: 0.22 }, {
      kind: "ellipse",
      width: 43,
      height: 76,
      fill: "rgba(0,0,0,0)",
      stroke: palette.background,
      strokeWidth: 2,
      zIndex: 510,
    }, 0.03),
    ...particleBurst("motion-design", duration, 0.85, 0.98, { x: 50, y: 48 }, [palette.background, palette.foreground, palette.accent2], 14, 520),
  );

  const captions: GeneratedMotionCaption[] = [
    caption("motion-design", "kicker", copy.kicker, phaseTime(duration, 0.02), hookEnd, { x: 8, y: 12, scale: 1, rotation: 0, opacity: 1 }, {
      size: 13,
      color: palette.accent2,
      align: "left",
      fontFamily: "mono",
      fontWeight: 700,
      letterSpacing: 3,
      textTransform: "uppercase",
      maxWidth: 42,
      reveal: "characters",
      revealDuration: 0.46,
      zIndex: 80,
    }),
    caption("motion-design", "headline", copy.headline, phaseTime(duration, 0.035), hookEnd, { x: 8, y: 43, scale: 1, rotation: 0, opacity: 1 }, {
      size: 92,
      color: palette.foreground,
      align: "left",
      fontFamily: "display",
      fontWeight: 900,
      letterSpacing: -0.4,
      lineHeight: 0.86,
      maxWidth: 76,
      reveal: "words",
      revealDuration: 0.48,
      textShadowColor: "rgba(255,106,61,0.28)",
      textShadowBlur: 22,
      zIndex: 84,
    }, 0.03),
    caption("motion-design", "hook-meta", "PROMPT  /  SCENE  /  FRAME  /  EXPORT", phaseTime(duration, 0.1), hookEnd, { x: 8, y: 82, scale: 1, rotation: 0, opacity: 0.64 }, {
      size: 12,
      color: palette.foreground,
      align: "left",
      fontFamily: "mono",
      fontWeight: 650,
      letterSpacing: 1.8,
      maxWidth: 58,
      reveal: "wipe",
      revealDuration: 0.32,
      zIndex: 80,
    }, 0.08),

    caption("motion-design", "build-label", "MANOR / LIVE COMPOSITION", phaseTime(duration, 0.2), buildEnd, { x: 10, y: 10, scale: 1, rotation: 0, opacity: 1 }, {
      size: 12,
      color: palette.accent2,
      align: "left",
      fontFamily: "mono",
      fontWeight: 700,
      letterSpacing: 2.2,
      maxWidth: 42,
      zIndex: 90,
    }),
    caption("motion-design", "topbar-label", "MANOR.VIDEO  /  COMPOSITION 0042", phaseTime(duration, 0.235), buildEnd, { x: 18, y: 19.6, scale: 1, rotation: -1.2, opacity: 0.68 }, {
      size: 9,
      color: palette.foreground,
      align: "left",
      fontFamily: "mono",
      fontWeight: 650,
      letterSpacing: 1.1,
      maxWidth: 38,
      zIndex: 24,
    }, 0.04),
    caption("motion-design", "inspector-label", "MOTION", phaseTime(duration, 0.28), buildEnd, { x: 70, y: 27.3, scale: 1, rotation: -1.2, opacity: 0.72 }, {
      size: 9,
      color: palette.foreground,
      align: "left",
      fontFamily: "mono",
      fontWeight: 800,
      letterSpacing: 1.5,
      maxWidth: 12,
      zIndex: 24,
    }, 0.05),
    caption("motion-design", "timeline-timecode", "00:02.16  /  00:06.00", phaseTime(duration, 0.34), buildEnd, { x: 70.5, y: 66.8, scale: 1, rotation: -1.2, opacity: 0.54 }, {
      size: 8,
      color: palette.foreground,
      align: "right",
      fontFamily: "mono",
      fontWeight: 650,
      letterSpacing: 0.8,
      maxWidth: 18,
      zIndex: 26,
    }, 0.08),
    caption("motion-design", "canvas-title", "AI EDIT", phaseTime(duration, 0.26), buildEnd, { x: 44, y: 43, scale: 1, rotation: -1.2, opacity: 1 }, {
      size: 32,
      color: palette.foreground,
      align: "center",
      fontFamily: "display",
      fontWeight: 900,
      letterSpacing: 0.2,
      maxWidth: 28,
      reveal: "wipe",
      revealDuration: 0.28,
      textShadowColor: "rgba(255,106,61,0.34)",
      textShadowBlur: 16,
      zIndex: 32,
    }, 0.06),
    caption("motion-design", "canvas-subtitle", "SEEK-SAFE MOTION / 30 FPS", phaseTime(duration, 0.31), buildEnd, { x: 44, y: 52, scale: 1, rotation: -1.2, opacity: 0.7 }, {
      size: 10,
      color: palette.accent2,
      align: "center",
      fontFamily: "mono",
      fontWeight: 700,
      letterSpacing: 1.3,
      maxWidth: 30,
      reveal: "characters",
      revealDuration: 0.42,
      zIndex: 32,
    }, 0.08),

    caption("motion-design", "proof-kicker", "RENDER PROOF / 0042", proofStart, proofEnd, { x: 10, y: 23, scale: 1, rotation: 0, opacity: 1 }, {
      size: 12,
      color: "#685e54",
      align: "left",
      fontFamily: "mono",
      fontWeight: 700,
      letterSpacing: 2.1,
      maxWidth: 32,
      zIndex: 180,
    }),
    caption("motion-design", "stat", copy.statValue, phaseTime(duration, 0.58), proofEnd, { x: 10, y: 42, scale: 1, rotation: 0, opacity: 1 }, {
      size: 148,
      color: "#201d1a",
      align: "left",
      fontFamily: "display",
      fontWeight: 900,
      letterSpacing: -2,
      maxWidth: 34,
      reveal: "characters",
      revealDuration: 0.36,
      zIndex: 180,
    }, 0.04),
    caption("motion-design", "stat-label", copy.statLabel, phaseTime(duration, 0.62), proofEnd, { x: 11, y: 58, scale: 1, rotation: 0, opacity: 1 }, {
      size: 15,
      color: palette.accent,
      align: "left",
      fontFamily: "mono",
      fontWeight: 800,
      letterSpacing: 1.6,
      textTransform: "uppercase",
      maxWidth: 30,
      reveal: "wipe",
      revealDuration: 0.32,
      zIndex: 180,
    }, 0.06),
    caption("motion-design", "subhead", copy.subhead, phaseTime(duration, 0.64), proofEnd, { x: 11, y: 70, scale: 1, rotation: 0, opacity: 1 }, {
      size: 19,
      color: "#4d4741",
      align: "left",
      fontFamily: "sans",
      fontWeight: 520,
      lineHeight: 1.25,
      maxWidth: 32,
      reveal: "words",
      revealDuration: 0.56,
      zIndex: 180,
    }, 0.08),
    caption("motion-design", "chart-label", "OUTPUT VELOCITY", phaseTime(duration, 0.6), proofEnd, { x: 58, y: 20, scale: 1, rotation: 0, opacity: 0.8 }, {
      size: 12,
      color: "#4d4741",
      align: "left",
      fontFamily: "mono",
      fontWeight: 700,
      letterSpacing: 1.8,
      maxWidth: 30,
      zIndex: 180,
    }, 0.05),

    caption("motion-design", "resolve-kicker", "MANOR MOTION SYSTEM", phaseTime(duration, 0.84), duration, { x: 50, y: 27, scale: 1, rotation: 0, opacity: 1 }, {
      size: 13,
      color: palette.background,
      align: "center",
      fontFamily: "mono",
      fontWeight: 800,
      letterSpacing: 3,
      maxWidth: 46,
      reveal: "characters",
      revealDuration: 0.38,
      zIndex: 600,
    }),
    caption("motion-design", "cta", copy.cta, phaseTime(duration, 0.85), duration, { x: 50, y: 49, scale: 1, rotation: 0, opacity: 1 }, {
      size: 70,
      color: palette.background,
      align: "center",
      fontFamily: "display",
      fontWeight: 900,
      letterSpacing: -0.4,
      maxWidth: 76,
      reveal: "wipe",
      revealDuration: 0.3,
      zIndex: 610,
    }, 0.03),
    caption("motion-design", "resolve-meta", "EDITABLE  ·  DETERMINISTIC  ·  YOURS", phaseTime(duration, 0.89), duration, { x: 50, y: 72, scale: 1, rotation: 0, opacity: 0.78 }, {
      size: 12,
      color: palette.background,
      align: "center",
      fontFamily: "mono",
      fontWeight: 700,
      letterSpacing: 2,
      maxWidth: 64,
      zIndex: 610,
    }, 0.05),
  ];
  return { captions, graphics };
}

function buildTextureLaunch(
  duration: number,
  palette: MotionDesignPalette,
  copy: typeof DEFAULT_COPY[MotionDesignPreset],
): Pick<GeneratedMotionDesign, "captions" | "graphics"> {
  const preset: MotionDesignPreset = "texture-launch";
  const textureStart = phaseTime(duration, 0.13);
  const textureEnd = phaseTime(duration, 0.39);
  const warpStart = phaseTime(duration, 0.38);
  const warpEnd = phaseTime(duration, 0.73);
  const resolveStart = phaseTime(duration, 0.7);

  const cutGraphic = (
    name: string,
    start: number,
    end: number,
    options: Partial<GeneratedMotionGraphic>,
  ): GeneratedMotionGraphic => {
    const layer = graphic(preset, name, start, end, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 1 }, options);
    layer.keyframes = [];
    return layer;
  };

  const graphics: GeneratedMotionGraphic[] = [
    cutGraphic("background", 0, duration, {
      width: 100,
      height: 100,
      fill: palette.background,
      fillType: "radial",
      fillSecondary: "#d9d3c6",
      effect: "grain",
      effectStrength: 0.36,
      zIndex: -100,
    }),
    cutGraphic("intro-volumetric-light", 0, textureStart, {
      kind: "shader",
      width: 100,
      height: 100,
      fill: "#17110d",
      fillSecondary: "#7b3f2c",
      stroke: "#ffd894",
      shaderPreset: "volumetricFog",
      shaderSeed: 304,
      shaderSpeed: 0.62,
      shaderScale: 1.14,
      shaderIntensity: 1.16,
      shaderBloom: 0.74,
      shaderGrain: 0.075,
      shaderCameraX: -0.35,
      shaderCameraY: 0.16,
      shaderCameraZ: 0.82,
      blendMode: "multiply",
      opacity: 0.38,
      zIndex: -80,
    }),
    withAxisReveal(graphic(preset, "intro-rule", phaseTime(duration, 0.015), phaseTime(duration, 0.15), { x: 29, y: 18, scale: 1, rotation: 0, opacity: 1 }, {
      width: 46,
      height: 0.65,
      fill: palette.accent,
      zIndex: 20,
    }), "x", 0.02),
    graphic(preset, "intro-register", phaseTime(duration, 0.035), phaseTime(duration, 0.15), { x: 87, y: 76, scale: 1, rotation: 0, opacity: 0.7 }, {
      kind: "path",
      width: 8,
      height: 14,
      fill: "rgba(0,0,0,0)",
      stroke: palette.foreground,
      strokeWidth: 1.2,
      pathData: "M 50 0 L 50 100 M 0 50 L 100 50 M 18 18 L 82 82 M 82 18 L 18 82",
      zIndex: 22,
    }, 0.08),
    opticalTransition(preset, duration, "intro-burn", 0.105, 0.155, "filmBurn", -15, 72, 0.95, 0.9, 880),
  ];

  const textureLooks: Array<{
    word: string;
    bg: string;
    fg: string;
    accent: string;
    effect: GraphicVisualStyle["effect"];
  }> = [
    { word: "PAPER", bg: "#f0ddae", fg: "#17110c", accent: "#d6532d", effect: "grain" },
    { word: "METAL", bg: "#b9c4d4", fg: "#111923", accent: "#f5fbff", effect: "halation" },
    { word: "STONE", bg: "#6f655d", fg: "#f4ede5", accent: "#281f1a", effect: "vignette" },
    { word: "INK", bg: "#e9d7f5", fg: "#201128", accent: "#ff4e2e", effect: "chromatic" },
    { word: "GLASS", bg: "#072b32", fg: "#e9fbff", accent: "#45d7ff", effect: "glass" },
    { word: "NOISE", bg: "#f04a36", fg: "#fff4df", accent: "#19110e", effect: "scanlines" },
    { word: "LIGHT", bg: "#16151f", fg: "#fff7d5", accent: "#ffd45d", effect: "lightLeak" },
  ];
  const lookDuration = (textureEnd - textureStart) / textureLooks.length;
  textureLooks.forEach((look, index) => {
    const start = textureStart + index * lookDuration;
    const end = index === textureLooks.length - 1 ? textureEnd : start + lookDuration + 0.025;
    graphics.push(
      cutGraphic(`look-${index + 1}-background`, start, end, {
        kind: "shader",
        width: 100,
        height: 100,
        fill: look.bg,
        fillSecondary: look.accent,
        stroke: look.fg,
        shaderPreset: index === 0
          ? "inkBloom"
          : index === 1
            ? "liquidMetal"
            : index === 3
              ? "inkBloom"
              : index === 4
                ? "prismaticBurst"
                : index === 5
                  ? "filmBurn"
                  : index === 6
                    ? "volumetricFog"
                    : "domainWarp",
        shaderSeed: 411 + index * 97,
        shaderSpeed: 0.8 + index * 0.11,
        shaderScale: 0.78 + (index % 3) * 0.24,
        shaderIntensity: 0.92 + (index % 2) * 0.18,
        shaderBloom: index === 1 || index === 4 || index === 6 ? 0.88 : 0.42,
        shaderGrain: 0.055 + (index % 3) * 0.018,
        shaderCameraX: (index % 3 - 1) * 0.18,
        shaderCameraY: (index % 2 ? 1 : -1) * 0.12,
        shaderCameraZ: 0.82 + (index % 3) * 0.14,
        effect: look.effect,
        effectStrength: 0.42,
        zIndex: 40 + index,
      }),
      cutGraphic(`look-${index + 1}-stripe-a`, start, end, {
        width: 118,
        height: 12 + (index % 3) * 5,
        fill: look.accent,
        rotation: -18 + index * 7,
        opacity: 0.26,
        blendMode: index % 2 === 0 ? "multiply" : "screen",
        effect: "grain",
        effectStrength: 0.58,
        zIndex: 55 + index,
      }),
      cutGraphic(`look-${index + 1}-frame`, start, end, {
        width: 88,
        height: 76,
        fill: "rgba(0,0,0,0)",
        stroke: look.fg,
        strokeWidth: 0.7,
        opacity: 0.42,
        zIndex: 62 + index,
      }),
    );
  });

  graphics.push(
    opticalTransition(preset, duration, "texture-rip-flare", 0.355, 0.41, "anamorphic", -10, 94, 0.82, 0.86, 890),
    cutGraphic("warp-background", warpStart, warpEnd, {
      kind: "shader",
      width: 100,
      height: 100,
      fill: "#06070c",
      fillSecondary: palette.accent,
      stroke: palette.accent2,
      shaderPreset: "parallaxGrid",
      shaderSeed: 7319,
      shaderSpeed: 0.72,
      shaderScale: 1.18,
      shaderIntensity: 1.28,
      shaderBloom: 1.12,
      shaderGrain: 0.065,
      shaderCameraX: -0.16,
      shaderCameraY: 0.12,
      shaderCameraZ: 0.72,
      effect: "vignette",
      effectStrength: 0.46,
      zIndex: 120,
    }),
    cutGraphic("warp-chromatic-depth", phaseTime(duration, 0.43), warpEnd, {
      kind: "shader",
      width: 100,
      height: 100,
      fill: "#02030a",
      fillSecondary: palette.accent2,
      stroke: palette.accent,
      shaderPreset: "prismaticBurst",
      shaderSeed: 8142,
      shaderSpeed: 1.16,
      shaderScale: 0.82,
      shaderIntensity: 0.94,
      shaderBloom: 0.92,
      shaderGrain: 0.04,
      shaderCameraX: 0.38,
      shaderCameraY: -0.18,
      shaderCameraZ: 1.32,
      blendMode: "screen",
      opacity: 0.34,
      zIndex: 124,
    }),
    withCameraDrift(graphic(preset, "warp-orb-cyan", warpStart, warpEnd, { x: 24, y: 36, scale: 1, rotation: 0, opacity: 0.72 }, {
      kind: "ellipse",
      width: 48,
      height: 72,
      fill: palette.accent2,
      fillType: "radial",
      fillSecondary: "rgba(69,215,255,0)",
      blur: 22,
      blendMode: "screen",
      effect: "glow",
      effectStrength: 0.78,
      zIndex: 130,
    }), 13, 8, 1.34),
    withCameraDrift(graphic(preset, "warp-orb-red", warpStart, warpEnd, { x: 78, y: 66, scale: 1, rotation: 0, opacity: 0.68 }, {
      kind: "ellipse",
      width: 52,
      height: 76,
      fill: palette.accent,
      fillType: "radial",
      fillSecondary: "rgba(255,79,46,0)",
      blur: 24,
      blendMode: "screen",
      effect: "halation",
      effectStrength: 0.82,
      zIndex: 132,
    }), -15, -6, 1.3),
    graphic(preset, "warp-glass", phaseTime(duration, 0.42), warpEnd, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 0.94 }, {
      width: 82,
      height: 68,
      fill: "rgba(255,255,255,0.055)",
      fillType: "linear",
      fillSecondary: "rgba(255,255,255,0.015)",
      gradientAngle: 142,
      stroke: "rgba(255,255,255,0.24)",
      strokeWidth: 0.7,
      cornerRadius: 7,
      effect: "glass",
      effectStrength: 0.74,
      shadowColor: "rgba(0,0,0,0.58)",
      shadowBlur: 46,
      shadowOffsetY: 22,
      zIndex: 180,
    }, 0.04),
    graphic(preset, "warp-particles", phaseTime(duration, 0.4), warpEnd, { x: 50, y: 50, scale: 1, rotation: 0, opacity: 0.86 }, {
      kind: "particle",
      width: 92,
      height: 82,
      fill: palette.accent2,
      fillSecondary: palette.accent,
      stroke: "#fff4ce",
      particleCount: 34,
      particleShape: "spark",
      particleMotion: "orbit",
      particleSeed: 7319,
      particleSize: 0.72,
      particleSpeed: 0.82,
      particleGravity: 0,
      particleSpread: 1.08,
      particleLoop: true,
      blendMode: "screen",
      zIndex: 166,
    }),
  );

  for (let index = 0; index < 13; index += 1) {
    const ribbon = graphic(preset, `warp-ribbon-${index + 1}`, phaseTime(duration, 0.44 + index * 0.006), warpEnd, {
      x: 50,
      y: 20 + index * 5.1,
      scale: 1,
      rotation: -7 + index * 1.15,
      opacity: 0.12 + (index % 4) * 0.05,
    }, {
      width: 118,
      height: 1.1 + (index % 3) * 0.55,
      fill: index % 2 === 0 ? palette.accent2 : palette.accent,
      fillType: "linear",
      fillSecondary: index % 2 === 0 ? "rgba(255,79,46,0)" : "rgba(69,215,255,0)",
      gradientAngle: index % 2 === 0 ? 0 : 180,
      blur: index % 3 === 0 ? 2.2 : 0,
      blendMode: "screen",
      effect: index % 3 === 0 ? "glow" : "chromatic",
      effectStrength: 0.32 + (index % 4) * 0.1,
      zIndex: 150 + index,
    });
    const localDuration = Math.max(0.1, ribbon.end - ribbon.start);
    ribbon.keyframes = [
      motionFrame(`${ribbon.id}-in`, 0, { x: -12 - index * 2, y: ribbon.y + 4, scale: 0.82, rotation: ribbon.rotation - 9, opacity: 0 }, "hold"),
      motionFrame(`${ribbon.id}-crest`, localDuration * 0.46, { x: 48 + (index % 3) * 4, y: ribbon.y - 3, scale: 1.06, rotation: ribbon.rotation + 4, opacity: ribbon.opacity }, "easeInOut", "smooth"),
      motionFrame(`${ribbon.id}-out`, localDuration, { x: 112 + index * 1.5, y: ribbon.y + 2, scale: 1.16, rotation: ribbon.rotation + 11, opacity: 0 }, "easeOut", "smooth"),
    ];
    graphics.push(ribbon);
  }

  graphics.push(
    opticalTransition(preset, duration, "warp-out", 0.68, 0.74, "filmBurn", -20, 88, 0.98, 0.92, 900),
    cutGraphic("resolve-background", resolveStart, duration, {
      kind: "shader",
      width: 100,
      height: 100,
      fill: "#f4efe3",
      fillSecondary: "#d9d1c2",
      stroke: "#fff8df",
      shaderPreset: "inkBloom",
      shaderSeed: 2206,
      shaderSpeed: 0.24,
      shaderScale: 0.72,
      shaderIntensity: 1.2,
      shaderBloom: 0.28,
      shaderGrain: 0.05,
      shaderCameraX: -0.28,
      shaderCameraY: 0.18,
      shaderCameraZ: 1.48,
      effect: "grain",
      effectStrength: 0.18,
      zIndex: 400,
    }),
    withAxisReveal(graphic(preset, "resolve-rule", phaseTime(duration, 0.705), duration, { x: 50, y: 68, scale: 1, rotation: 0, opacity: 1 }, {
      width: 58,
      height: 0.75,
      fill: palette.accent,
      zIndex: 520,
    }), "x", 0.04),
    graphic(preset, "resolve-seal", phaseTime(duration, 0.715), duration, { x: 84, y: 22, scale: 1, rotation: 0, opacity: 1 }, {
      kind: "ellipse",
      width: 8.5,
      height: 15,
      fill: palette.accent,
      fillType: "linear",
      fillSecondary: palette.accent2,
      gradientAngle: 45,
      effect: "glow",
      effectStrength: 0.45,
      zIndex: 520,
    }, 0.04),
    ...particleBurst(preset, duration, 0.71, 0.95, { x: 84, y: 22 }, [palette.accent, palette.accent2, "#151515", "#f4c95d"], 16, 510),
    cutGraphic("master-vignette", 0, duration, {
      width: 100,
      height: 100,
      fill: "rgba(0,0,0,0)",
      effect: "vignette",
      effectStrength: 0.32,
      opacity: 0.6,
      zIndex: 940,
    }),
    cutGraphic("master-grain", 0, duration, {
      width: 100,
      height: 100,
      fill: "rgba(0,0,0,0)",
      effect: "grain",
      effectStrength: 0.44,
      blendMode: "soft-light",
      opacity: 0.32,
      zIndex: 950,
    }),
  );

  const captions: GeneratedMotionCaption[] = [
    caption(preset, "kicker", copy.kicker, phaseTime(duration, 0.012), phaseTime(duration, 0.15), { x: 8, y: 11, scale: 1, rotation: 0, opacity: 1 }, {
      size: 17,
      color: palette.accent,
      align: "left",
      fontFamily: "mono",
      fontWeight: 700,
      letterSpacing: 2.8,
      textTransform: "uppercase",
      maxWidth: 56,
      reveal: "characters",
      revealDuration: 0.58,
      zIndex: 100,
    }),
    caption(preset, "headline", copy.headline, phaseTime(duration, 0.025), phaseTime(duration, 0.15), { x: 8, y: 44, scale: 1, rotation: 0, opacity: 1 }, {
      size: 172,
      color: palette.foreground,
      align: "left",
      fontFamily: "display",
      fontWeight: 900,
      letterSpacing: -4.2,
      lineHeight: 0.82,
      maxWidth: 84,
      reveal: "words",
      revealDuration: 0.5,
      zIndex: 102,
    }, 0.03),
    caption(preset, "intro-meta", "1920×1080  /  30 FPS  /  NO VIDEO MODEL", phaseTime(duration, 0.055), phaseTime(duration, 0.15), { x: 8, y: 83, scale: 1, rotation: 0, opacity: 0.72 }, {
      size: 13,
      color: palette.foreground,
      align: "left",
      fontFamily: "mono",
      fontWeight: 600,
      letterSpacing: 1.7,
      maxWidth: 74,
      zIndex: 104,
    }, 0.08),
  ];

  textureLooks.forEach((look, index) => {
    const start = textureStart + index * lookDuration;
    const end = index === textureLooks.length - 1 ? textureEnd : start + lookDuration;
    captions.push(caption(preset, `look-word-${index + 1}`, look.word, start, end, { x: 50, y: 50, scale: 1, rotation: index % 2 === 0 ? -4 : 3, opacity: 1 }, {
      size: index === 1 ? 350 : 318,
      color: look.fg,
      align: "center",
      fontFamily: index % 3 === 0 ? "display" : "sans",
      fontWeight: 900,
      letterSpacing: index % 2 === 0 ? -3.5 : 6,
      textTransform: "uppercase",
      textShadowColor: look.accent,
      textShadowBlur: index === 4 || index === 6 ? 34 : 12,
      strokeColor: look.accent,
      strokeWidth: index === 1 ? 1.5 : 0,
      maxWidth: 92,
      zIndex: 100 + index,
    }));
  });

  captions.push(
    caption(preset, "warp-kicker", "NATIVE CANVAS / SEEK-SAFE", phaseTime(duration, 0.405), warpEnd, { x: 50, y: 22, scale: 1, rotation: 0, opacity: 0.8 }, {
      size: 14,
      color: palette.accent2,
      align: "center",
      fontFamily: "mono",
      fontWeight: 700,
      letterSpacing: 2.6,
      maxWidth: 62,
      reveal: "characters",
      revealDuration: 0.58,
      zIndex: 260,
    }),
    caption(preset, "warp-title", "MATERIAL\nBECOMES MOTION", phaseTime(duration, 0.42), phaseTime(duration, 0.68), { x: 50, y: 49, scale: 1, rotation: 0, opacity: 1 }, {
      size: 164,
      color: "#ffffff",
      align: "center",
      fontFamily: "display",
      fontWeight: 900,
      letterSpacing: -3,
      lineHeight: 0.86,
      maxWidth: 76,
      reveal: "wipe",
      revealDuration: 0.5,
      textShadowColor: "rgba(69,215,255,0.72)",
      textShadowBlur: 34,
      zIndex: 270,
    }, 0.04),
    caption(preset, "warp-subhead", copy.subhead, phaseTime(duration, 0.55), warpEnd, { x: 50, y: 75, scale: 1, rotation: 0, opacity: 0.82 }, {
      size: 22,
      color: "#f8fafc",
      align: "center",
      fontFamily: "sans",
      fontWeight: 500,
      maxWidth: 62,
      reveal: "words",
      revealDuration: 0.7,
      zIndex: 270,
    }, 0.06),
    caption(preset, "resolve-kicker", copy.statLabel, phaseTime(duration, 0.705), duration, { x: 50, y: 24, scale: 1, rotation: 0, opacity: 0.72 }, {
      size: 13,
      color: palette.foreground,
      align: "center",
      fontFamily: "mono",
      fontWeight: 700,
      letterSpacing: 2.5,
      maxWidth: 64,
      zIndex: 610,
    }, 0.02),
    caption(preset, "cta", copy.cta, phaseTime(duration, 0.7), duration, { x: 50, y: 46, scale: 1, rotation: 0, opacity: 1 }, {
      size: 176,
      color: palette.foreground,
      align: "center",
      fontFamily: "display",
      fontWeight: 900,
      letterSpacing: -3.2,
      lineHeight: 0.86,
      maxWidth: 82,
      reveal: "wipe",
      revealDuration: 0.2,
      zIndex: 620,
    }, 0.04),
    caption(preset, "resolve-meta", "CODE  ·  KEYFRAMES  ·  PARTICLES  ·  EFFECTS", phaseTime(duration, 0.76), duration, { x: 50, y: 76, scale: 1, rotation: 0, opacity: 0.8 }, {
      size: 14,
      color: palette.foreground,
      align: "center",
      fontFamily: "mono",
      fontWeight: 700,
      letterSpacing: 2.2,
      maxWidth: 78,
      reveal: "characters",
      revealDuration: 0.72,
      zIndex: 620,
    }, 0.08),
    caption(preset, "resolve-proof", copy.statValue, phaseTime(duration, 0.8), duration, { x: 88, y: 87, scale: 1, rotation: 0, opacity: 0.62 }, {
      size: 28,
      color: palette.accent,
      align: "right",
      fontFamily: "display",
      fontWeight: 900,
      maxWidth: 18,
      zIndex: 625,
    }, 0.08),
  );

  return { captions, graphics };
}

export function isMotionDesignPreset(value: unknown): value is MotionDesignPreset {
  return typeof value === "string" && (MOTION_DESIGN_PRESETS as readonly string[]).includes(value);
}

export function createMotionDesignComposition(
  request: MotionDesignRequest | undefined,
  duration: number,
): GeneratedMotionDesign | null {
  if (!request || !isMotionDesignPreset(request.preset) || !Number.isFinite(duration) || duration < 0.5) return null;
  const preset = request.preset;
  const defaults = DEFAULT_COPY[preset];
  const basePalette = PRESET_PALETTES[preset];
  const palette: MotionDesignPalette = {
    background: request.background || basePalette.background,
    surface: request.surface || basePalette.surface,
    foreground: request.foreground || basePalette.foreground,
    accent: request.accent || basePalette.accent,
    accent2: request.accent2 || basePalette.accent2,
  };
  const copy = {
    headline: safeText(request.headline, defaults.headline),
    kicker: safeText(request.kicker, defaults.kicker),
    subhead: safeText(request.subhead, defaults.subhead),
    cta: safeText(request.cta, defaults.cta),
    statValue: safeText(request.statValue, defaults.statValue),
    statLabel: safeText(request.statLabel, defaults.statLabel),
  };
  const builders: Record<MotionDesignPreset, (
    compositionDuration: number,
    compositionPalette: MotionDesignPalette,
    compositionCopy: typeof copy,
  ) => Pick<GeneratedMotionDesign, "captions" | "graphics">> = {
    "kinetic-type": buildKineticType,
    "swiss-grid": buildSwissGrid,
    "product-promo": buildProductPromo,
    "editorial-data": buildEditorialData,
    "product-film": buildProductFilm,
    "feature-reveal": buildFeatureReveal,
    "product-story": buildProductStory,
    "motion-design": buildMotionDesign,
    "texture-launch": buildTextureLaunch,
  };
  const builtLayers = builders[preset](duration, palette, copy);
  const requestedFont = request.fontFamily
    || (request.visualTone === "technical" ? "mono" : request.visualTone === "editorial" ? "display" : undefined);
  const draft = request.quality === "draft";
  const layers = {
    captions: builtLayers.captions.map((item) => {
      const isPrimaryType = /-(headline|cta)$/.test(item.id);
      return {
        ...item,
        ...(requestedFont && isPrimaryType ? { fontFamily: requestedFont } : {}),
        ...(draft ? { textShadowBlur: 0, strokeWidth: 0, reveal: "none" as const } : {}),
      };
    }),
    graphics: builtLayers.graphics.map((item) => ({
      ...item,
      ...(draft ? { shadowBlur: 0, shadowOffsetX: 0, shadowOffsetY: 0, blur: 0, blendMode: "normal" as const, effect: "none" as const } : {}),
    })),
  };
  const audioCues: GeneratedMotionDesign["audioCues"] = preset === "texture-launch" ? [
    {
      id: `${preset}-score`,
      type: "music",
      label: "Procedural cinematic score",
      start: 0,
      end: duration,
      volumeDb: -10,
      fadeIn: Math.min(1.2, duration * 0.08),
      fadeOut: Math.min(1.6, duration * 0.1),
      loop: true,
      duckUnderDialogue: true,
      muted: false,
      sourceStart: 0,
      sourceEnd: 4,
      assetDuration: null,
      assetDocumentId: null,
      assetName: null,
      assetMimeType: null,
      sourcePlan: "Native deterministic synth: pulse, bass, pad, percussion",
      prompt: "Cinematic electronic pulse at 120 BPM; no vocals; edit-safe four-second loop.",
    },
    {
      id: `${preset}-room-tone`,
      type: "ambience",
      label: "Textural room tone",
      start: 0,
      end: duration,
      volumeDb: -23,
      fadeIn: Math.min(1.8, duration * 0.1),
      fadeOut: Math.min(2, duration * 0.12),
      loop: true,
      duckUnderDialogue: false,
      muted: false,
      sourceStart: 0,
      sourceEnd: 4,
      assetDuration: null,
      assetDocumentId: null,
      assetName: null,
      assetMimeType: null,
      sourcePlan: "Native deterministic low-frequency ambience",
      prompt: "Subtle analog room tone with slow drift; no vocals.",
    },
    ...[
      { name: "Texture rip", start: phaseTime(duration, 0.125), end: phaseTime(duration, 0.18), volume: -8 },
      { name: "Warp impact", start: phaseTime(duration, 0.385), end: phaseTime(duration, 0.455), volume: -6 },
      { name: "Resolve hit", start: phaseTime(duration, 0.695), end: phaseTime(duration, 0.775), volume: -7 },
    ].map((cue, index) => ({
      id: `${preset}-sfx-${index + 1}`,
      type: "sfx" as const,
      label: cue.name,
      start: cue.start,
      end: Math.max(cue.start + 0.3, cue.end),
      volumeDb: cue.volume,
      fadeIn: 0.01,
      fadeOut: 0.18,
      loop: false,
      duckUnderDialogue: false,
      muted: false,
      sourceStart: 0,
      sourceEnd: 4,
      assetDuration: null,
      assetDocumentId: null,
      assetName: null,
      assetMimeType: null,
      sourcePlan: "Native deterministic transition synthesizer",
      prompt: `${cue.name}: short tonal sweep and transient; no vocals.`,
    })),
  ] : [];
  return {
    preset,
    ...layers,
    audioCues,
    ...baseSceneData(preset, duration, palette),
  };
}
