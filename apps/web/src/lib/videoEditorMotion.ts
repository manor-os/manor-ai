export const VIDEO_EDITOR_FPS = 30;
export const MOTION_EASINGS = [
  "linear",
  "hold",
  "gentle",
  "easeIn",
  "easeOut",
  "easeInOut",
  "snappy",
  "spring",
  "custom",
] as const;

export type MotionEasing = typeof MOTION_EASINGS[number];
export type MotionPreset = "fadeIn" | "slideUp" | "pop" | "kenBurns";
export const MOTION_PATH_MODES = ["linear", "smooth"] as const;
export type MotionPathMode = typeof MOTION_PATH_MODES[number];

export type MotionBezier = {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
};

export const DEFAULT_MOTION_BEZIER: Readonly<MotionBezier> = Object.freeze({
  x1: 0.25,
  y1: 0.1,
  x2: 0.25,
  y2: 1,
});

export type MotionPose = {
  x: number;
  y: number;
  scale: number;
  /** Optional axis multipliers enable bar wipes, squash/stretch, and camera depth without layout mutation. */
  scaleX?: number;
  scaleY?: number;
  /** Seek-safe depth tilt. Renderers project these values into their native 2D/3D transform surface. */
  rotationX?: number;
  rotationY?: number;
  /** Perspective distance in CSS pixels. Lower values increase the perceived depth. */
  perspective?: number;
  /** Render-time blur in design pixels, interpolated with the same easing as transforms. */
  blur?: number;
  /** Optional visual-effect intensity for graphic layers. */
  effectStrength?: number;
  /** Seek-safe SVG/path reveal progress. Zero hides the stroke; one draws it completely. */
  pathProgress?: number;
  rotation: number;
  opacity: number;
};

export type MotionKeyframe = MotionPose & {
  id: string;
  /** Seconds from the owning layer's start time. */
  time: number;
  /** Easing used while arriving at this pose. */
  easing: MotionEasing;
  /** Control points used when easing is custom. X is time; Y is progress. */
  bezier?: MotionBezier;
  /** Spatial interpolation used while arriving at this pose. */
  spatial?: MotionPathMode;
};

const KEYFRAME_TIME_EPSILON = 0.005;

function clamp(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min;
  return Math.min(max, Math.max(min, value));
}

function finiteOr(value: unknown, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function normalizePose(value: Partial<MotionPose> | undefined, fallback: MotionPose): MotionPose {
  const pose: MotionPose = {
    x: clamp(finiteOr(value?.x, fallback.x), 0, 100),
    y: clamp(finiteOr(value?.y, fallback.y), 0, 100),
    scale: clamp(finiteOr(value?.scale, fallback.scale), 0.1, 4),
    rotation: clamp(finiteOr(value?.rotation, fallback.rotation), -360, 360),
    opacity: clamp(finiteOr(value?.opacity, fallback.opacity), 0, 1),
  };
  if (value?.scaleX !== undefined || fallback.scaleX !== undefined) {
    pose.scaleX = clamp(finiteOr(value?.scaleX, fallback.scaleX ?? 1), 0.01, 8);
  }
  if (value?.scaleY !== undefined || fallback.scaleY !== undefined) {
    pose.scaleY = clamp(finiteOr(value?.scaleY, fallback.scaleY ?? 1), 0.01, 8);
  }
  if (value?.rotationX !== undefined || fallback.rotationX !== undefined) {
    pose.rotationX = clamp(finiteOr(value?.rotationX, fallback.rotationX ?? 0), -180, 180);
  }
  if (value?.rotationY !== undefined || fallback.rotationY !== undefined) {
    pose.rotationY = clamp(finiteOr(value?.rotationY, fallback.rotationY ?? 0), -180, 180);
  }
  if (value?.perspective !== undefined || fallback.perspective !== undefined) {
    pose.perspective = clamp(finiteOr(value?.perspective, fallback.perspective ?? 1200), 200, 4000);
  }
  if (value?.blur !== undefined || fallback.blur !== undefined) {
    pose.blur = clamp(finiteOr(value?.blur, fallback.blur ?? 0), 0, 80);
  }
  if (value?.effectStrength !== undefined || fallback.effectStrength !== undefined) {
    pose.effectStrength = clamp(finiteOr(value?.effectStrength, fallback.effectStrength ?? 0), 0, 1);
  }
  if (value?.pathProgress !== undefined || fallback.pathProgress !== undefined) {
    pose.pathProgress = clamp(finiteOr(value?.pathProgress, fallback.pathProgress ?? 1), 0, 1);
  }
  return pose;
}

function isMotionEasing(value: unknown): value is MotionEasing {
  return typeof value === "string" && (MOTION_EASINGS as readonly string[]).includes(value);
}

function isMotionPathMode(value: unknown): value is MotionPathMode {
  return value === "linear" || value === "smooth";
}

export function normalizeMotionBezier(value: Partial<MotionBezier> | undefined): MotionBezier {
  return {
    x1: clamp(finiteOr(value?.x1, DEFAULT_MOTION_BEZIER.x1), 0, 1),
    y1: clamp(finiteOr(value?.y1, DEFAULT_MOTION_BEZIER.y1), -1, 2),
    x2: clamp(finiteOr(value?.x2, DEFAULT_MOTION_BEZIER.x2), 0, 1),
    y2: clamp(finiteOr(value?.y2, DEFAULT_MOTION_BEZIER.y2), -1, 2),
  };
}

function cubicBezierCoordinate(parameter: number, firstControl: number, secondControl: number): number {
  const inverse = 1 - parameter;
  return 3 * inverse * inverse * parameter * firstControl
    + 3 * inverse * parameter * parameter * secondControl
    + parameter * parameter * parameter;
}

function cubicBezierDerivative(parameter: number, firstControl: number, secondControl: number): number {
  const inverse = 1 - parameter;
  return 3 * inverse * inverse * firstControl
    + 6 * inverse * parameter * (secondControl - firstControl)
    + 3 * parameter * parameter * (1 - secondControl);
}

/** CSS-compatible cubic-bezier solved with a fixed iteration count for deterministic seeks. */
export function cubicBezierProgress(progress: number, value?: Partial<MotionBezier>): number {
  const safeProgress = clamp(progress, 0, 1);
  if (safeProgress === 0 || safeProgress === 1) return safeProgress;
  const bezier = normalizeMotionBezier(value);
  let parameter = safeProgress;

  for (let iteration = 0; iteration < 8; iteration += 1) {
    const xError = cubicBezierCoordinate(parameter, bezier.x1, bezier.x2) - safeProgress;
    const slope = cubicBezierDerivative(parameter, bezier.x1, bezier.x2);
    if (Math.abs(slope) < 1e-7) break;
    parameter = clamp(parameter - xError / slope, 0, 1);
  }

  let lower = 0;
  let upper = 1;
  for (let iteration = 0; iteration < 24; iteration += 1) {
    const x = cubicBezierCoordinate(parameter, bezier.x1, bezier.x2);
    if (x < safeProgress) lower = parameter;
    else upper = parameter;
    parameter = (lower + upper) / 2;
  }
  return cubicBezierCoordinate(parameter, bezier.y1, bezier.y2);
}

/** Pure, seek-safe progress used identically by preview scrubbing and frame export. */
export function motionEasingProgress(
  easing: MotionEasing,
  progress: number,
  bezier?: Partial<MotionBezier>,
): number {
  const safe = clamp(progress, 0, 1);
  if (safe === 0 || safe === 1) return safe;
  if (easing === "hold") return 0;
  if (easing === "gentle") return 0.5 - Math.cos(Math.PI * safe) / 2;
  if (easing === "easeIn") return safe ** 3;
  if (easing === "easeOut") return 1 - (1 - safe) ** 3;
  if (easing === "easeInOut") {
    return safe < 0.5
      ? 4 * safe ** 3
      : 1 - ((-2 * safe + 2) ** 3) / 2;
  }
  if (easing === "snappy") return 1 - (1 - safe) ** 4;
  if (easing === "spring") {
    // Critically damped closed-form spring. It has no accumulated velocity, so
    // seeking any frame directly produces the same pose as linear playback.
    const decay = 8;
    const position = 1 - Math.exp(-decay * safe) * (1 + decay * safe);
    const endPosition = 1 - Math.exp(-decay) * (1 + decay);
    return position / endPosition;
  }
  if (easing === "custom") return cubicBezierProgress(safe, bezier);
  return safe;
}

export function motionFrameNumberAtTime(time: number, fps = VIDEO_EDITOR_FPS): number {
  const safeFps = Math.max(1, Math.round(finiteOr(fps, VIDEO_EDITOR_FPS)));
  return Math.max(0, Math.round(Math.max(0, finiteOr(time, 0)) * safeFps));
}

export function snapMotionTimeToFrame(
  time: number,
  maxTime = Number.POSITIVE_INFINITY,
  fps = VIDEO_EDITOR_FPS,
): number {
  const safeFps = Math.max(1, Math.round(finiteOr(fps, VIDEO_EDITOR_FPS)));
  const safeMaxTime = Number.isFinite(maxTime) ? Math.max(0, maxTime) : Number.POSITIVE_INFINITY;
  return clamp(motionFrameNumberAtTime(time, safeFps) / safeFps, 0, safeMaxTime);
}

export function normalizeMotionKeyframes(
  value: unknown,
  fallback: MotionPose,
  maxTime = Number.POSITIVE_INFINITY,
): MotionKeyframe[] {
  if (!Array.isArray(value)) return [];
  const safeMaxTime = Number.isFinite(maxTime) ? Math.max(0, maxTime) : Number.POSITIVE_INFINITY;
  const frames = value.flatMap((item, index): MotionKeyframe[] => {
    if (!item || typeof item !== "object") return [];
    const candidate = item as Partial<MotionKeyframe>;
    const pose = normalizePose(candidate, fallback);
    return [{
      ...pose,
      id: typeof candidate.id === "string" && candidate.id.trim() ? candidate.id : `motion-${index + 1}`,
      time: clamp(finiteOr(candidate.time, 0), 0, safeMaxTime),
      easing: isMotionEasing(candidate.easing) ? candidate.easing : "easeInOut",
      bezier: normalizeMotionBezier(candidate.bezier),
      spatial: isMotionPathMode(candidate.spatial) ? candidate.spatial : "linear",
    }];
  }).sort((left, right) => left.time - right.time);

  return frames.reduce<MotionKeyframe[]>((result, frame) => {
    const previous = result[result.length - 1];
    if (previous && Math.abs(previous.time - frame.time) <= KEYFRAME_TIME_EPSILON) {
      result[result.length - 1] = frame;
    } else {
      result.push(frame);
    }
    return result;
  }, []);
}

export function upsertMotionKeyframe(
  keyframes: MotionKeyframe[],
  keyframe: MotionKeyframe,
): MotionKeyframe[] {
  const existing = keyframes.find((frame) => Math.abs(frame.time - keyframe.time) <= KEYFRAME_TIME_EPSILON);
  const nextFrame = existing ? { ...existing, ...keyframe, id: existing.id } : keyframe;
  return [...keyframes.filter((frame) => frame.id !== existing?.id && frame.id !== keyframe.id), nextFrame]
    .sort((left, right) => left.time - right.time);
}

export function retimeMotionKeyframe(
  keyframes: MotionKeyframe[],
  id: string,
  time: number,
  maxTime = Number.POSITIVE_INFINITY,
  fps = VIDEO_EDITOR_FPS,
): MotionKeyframe[] {
  const target = keyframes.find((frame) => frame.id === id);
  if (!target) return keyframes;
  const snappedTime = snapMotionTimeToFrame(time, maxTime, fps);
  return [
    ...keyframes.filter((frame) => (
      frame.id !== id && Math.abs(frame.time - snappedTime) > KEYFRAME_TIME_EPSILON
    )),
    { ...target, time: snappedTime },
  ].sort((left, right) => left.time - right.time);
}

export function motionPoseAtTime(
  keyframes: MotionKeyframe[],
  time: number,
  fallback: MotionPose,
): MotionPose {
  if (keyframes.length === 0) return normalizePose(fallback, fallback);
  const frames = [...keyframes].sort((left, right) => left.time - right.time);
  const safeTime = finiteOr(time, 0);
  const first = frames[0];
  const last = frames[frames.length - 1];
  if (safeTime <= first.time) return normalizePose(first, fallback);
  if (safeTime >= last.time) return normalizePose(last, fallback);

  const rightIndex = frames.findIndex((frame) => frame.time >= safeTime);
  const right = frames[Math.max(1, rightIndex)];
  const left = frames[Math.max(0, rightIndex - 1)];
  const span = Math.max(KEYFRAME_TIME_EPSILON, right.time - left.time);
  const progress = motionEasingProgress(right.easing, (safeTime - left.time) / span, right.bezier);
  const from = normalizePose(left, fallback);
  const to = normalizePose(right, fallback);
  const interpolate = (start: number, end: number) => start + (end - start) * progress;
  let x = interpolate(from.x, to.x);
  let y = interpolate(from.y, to.y);
  if (right.spatial === "smooth") {
    const leftFrameIndex = Math.max(0, rightIndex - 1);
    const before = normalizePose(frames[Math.max(0, leftFrameIndex - 1)], fallback);
    const after = normalizePose(frames[Math.min(frames.length - 1, rightIndex + 1)], fallback);
    const squared = progress * progress;
    const cubed = squared * progress;
    const catmullRom = (p0: number, p1: number, p2: number, p3: number) => 0.5 * (
      (2 * p1)
      + (-p0 + p2) * progress
      + (2 * p0 - 5 * p1 + 4 * p2 - p3) * squared
      + (-p0 + 3 * p1 - 3 * p2 + p3) * cubed
    );
    x = catmullRom(before.x, from.x, to.x, after.x);
    y = catmullRom(before.y, from.y, to.y, after.y);
  }
  const pose: MotionPose = {
    x: clamp(x, 0, 100),
    y: clamp(y, 0, 100),
    scale: clamp(interpolate(from.scale, to.scale), 0.1, 4),
    rotation: interpolate(from.rotation, to.rotation),
    opacity: clamp(interpolate(from.opacity, to.opacity), 0, 1),
  };
  if (from.scaleX !== undefined || to.scaleX !== undefined) {
    pose.scaleX = clamp(interpolate(from.scaleX ?? 1, to.scaleX ?? 1), 0.01, 8);
  }
  if (from.scaleY !== undefined || to.scaleY !== undefined) {
    pose.scaleY = clamp(interpolate(from.scaleY ?? 1, to.scaleY ?? 1), 0.01, 8);
  }
  if (from.rotationX !== undefined || to.rotationX !== undefined) {
    pose.rotationX = clamp(interpolate(from.rotationX ?? 0, to.rotationX ?? 0), -180, 180);
  }
  if (from.rotationY !== undefined || to.rotationY !== undefined) {
    pose.rotationY = clamp(interpolate(from.rotationY ?? 0, to.rotationY ?? 0), -180, 180);
  }
  if (from.perspective !== undefined || to.perspective !== undefined) {
    pose.perspective = clamp(interpolate(from.perspective ?? 1200, to.perspective ?? 1200), 200, 4000);
  }
  if (from.blur !== undefined || to.blur !== undefined) {
    pose.blur = clamp(interpolate(from.blur ?? 0, to.blur ?? 0), 0, 80);
  }
  if (from.effectStrength !== undefined || to.effectStrength !== undefined) {
    pose.effectStrength = clamp(interpolate(from.effectStrength ?? 0, to.effectStrength ?? 0), 0, 1);
  }
  if (from.pathProgress !== undefined || to.pathProgress !== undefined) {
    pose.pathProgress = clamp(interpolate(from.pathProgress ?? 1, to.pathProgress ?? 1), 0, 1);
  }
  return pose;
}

export function motionPathSamples(
  keyframes: MotionKeyframe[],
  fallback: MotionPose,
  samplesPerSegment = 12,
): Array<{ time: number; x: number; y: number }> {
  if (keyframes.length === 0) return [];
  const frames = normalizeMotionKeyframes(keyframes, fallback);
  if (frames.length === 1) return [{ time: frames[0].time, x: frames[0].x, y: frames[0].y }];
  const sampleCount = Math.max(2, Math.round(finiteOr(samplesPerSegment, 12)));
  const samples: Array<{ time: number; x: number; y: number }> = [];
  for (let index = 1; index < frames.length; index += 1) {
    const left = frames[index - 1];
    const right = frames[index];
    for (let sample = 0; sample <= sampleCount; sample += 1) {
      if (index > 1 && sample === 0) continue;
      const progress = sample / sampleCount;
      const time = left.time + (right.time - left.time) * progress;
      const pose = motionPoseAtTime(frames, time, fallback);
      samples.push({ time, x: pose.x, y: pose.y });
    }
  }
  return samples;
}

export function createMotionPresetKeyframes(
  preset: MotionPreset,
  duration: number,
  basePose: MotionPose,
  idPrefix = "motion-preset",
): MotionKeyframe[] {
  const safeDuration = Math.max(0.05, finiteOr(duration, 0.05));
  const base = normalizePose(basePose, basePose);
  const frame = (
    suffix: string,
    time: number,
    patch: Partial<MotionPose>,
    easing: MotionEasing,
  ): MotionKeyframe => ({
    ...base,
    ...patch,
    id: `${idPrefix}-${suffix}`,
    time: clamp(time, 0, safeDuration),
    easing,
  });

  if (preset === "fadeIn") {
    return normalizeMotionKeyframes([
      frame("start", 0, { opacity: 0 }, "linear"),
      frame("settle", Math.min(0.45, safeDuration * 0.3), {}, "easeOut"),
    ], base, safeDuration);
  }
  if (preset === "slideUp") {
    return normalizeMotionKeyframes([
      frame("start", 0, { y: clamp(base.y + 8, 0, 100), scale: Math.max(0.1, base.scale * 0.98), opacity: 0 }, "linear"),
      frame("settle", Math.min(0.5, safeDuration * 0.34), {}, "easeOut"),
    ], base, safeDuration);
  }
  if (preset === "kenBurns") {
    return normalizeMotionKeyframes([
      frame("start", 0, {
        x: base.x - 1.25,
        y: base.y + 0.75,
      }, "linear"),
      frame("settle", safeDuration, {
        x: base.x + 1.25,
        y: base.y - 0.75,
        scale: base.scale * 1.08,
      }, "gentle"),
    ], base, safeDuration);
  }
  return normalizeMotionKeyframes([
    frame("start", 0, { scale: Math.max(0.1, base.scale * 0.78), opacity: 0 }, "linear"),
    frame("peak", Math.min(0.24, safeDuration * 0.18), { scale: Math.min(4, base.scale * 1.08) }, "snappy"),
    frame("settle", Math.min(0.48, safeDuration * 0.34), {}, "spring"),
  ], base, safeDuration);
}
