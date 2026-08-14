export const MIN_VIDEO_CLIP_SPEED = 0.25;
export const MAX_VIDEO_CLIP_SPEED = 4;
export const MAX_VIDEO_CLIP_FADE_SECONDS = 5;

function finiteOr(value: unknown, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function normalizeVideoClipSpeed(value: unknown): number {
  return Math.min(
    MAX_VIDEO_CLIP_SPEED,
    Math.max(MIN_VIDEO_CLIP_SPEED, finiteOr(value, 1)),
  );
}

export function videoClipTimelineDuration(
  sourceStart: number,
  sourceEnd: number,
  speed: unknown,
): number {
  const sourceDuration = Math.max(0, finiteOr(sourceEnd, 0) - finiteOr(sourceStart, 0));
  return sourceDuration / normalizeVideoClipSpeed(speed);
}

export function videoClipSourceTimeAtOffset(
  sourceStart: number,
  sourceEnd: number,
  speed: unknown,
  timelineOffset: number,
): number {
  const safeStart = finiteOr(sourceStart, 0);
  const safeEnd = Math.max(safeStart, finiteOr(sourceEnd, safeStart));
  const sourceTime = safeStart
    + Math.max(0, finiteOr(timelineOffset, 0)) * normalizeVideoClipSpeed(speed);
  return Math.min(safeEnd, sourceTime);
}

export function videoClipTimelineOffsetAtSourceTime(
  sourceStart: number,
  sourceEnd: number,
  speed: unknown,
  sourceTime: number,
): number {
  const safeStart = finiteOr(sourceStart, 0);
  const safeEnd = Math.max(safeStart, finiteOr(sourceEnd, safeStart));
  const safeSourceTime = Math.min(safeEnd, Math.max(safeStart, finiteOr(sourceTime, safeStart)));
  return (safeSourceTime - safeStart) / normalizeVideoClipSpeed(speed);
}

export function videoOverlaySourceWindow(
  assetDuration: unknown,
  sourceStart: unknown,
  sourceEnd: unknown,
  fallbackDuration: unknown,
): { start: number; end: number; duration: number } {
  const explicitAssetDuration = assetDuration == null ? 0 : finiteOr(assetDuration, 0);
  const explicitSourceEnd = sourceEnd == null
    ? finiteOr(fallbackDuration, 0.05)
    : finiteOr(sourceEnd, finiteOr(fallbackDuration, 0.05));
  const safeAssetDuration = explicitAssetDuration > 0
    ? explicitAssetDuration
    : Math.max(0.05, explicitSourceEnd);
  const start = Math.min(
    Math.max(0, safeAssetDuration - 0.01),
    Math.max(0, finiteOr(sourceStart, 0)),
  );
  const requestedSourceEnd = sourceEnd == null ? safeAssetDuration : finiteOr(sourceEnd, safeAssetDuration);
  const end = Math.min(
    safeAssetDuration,
    Math.max(start + 0.01, requestedSourceEnd),
  );
  return { start, end, duration: Math.max(0.01, end - start) };
}

export function videoOverlaySourceTimeAtTimelineTime({
  timelineStart,
  timelineTime,
  speed,
  loop,
  assetDuration,
  sourceStart,
  sourceEnd,
  fallbackDuration,
}: {
  timelineStart: unknown;
  timelineTime: unknown;
  speed: unknown;
  loop: unknown;
  assetDuration: unknown;
  sourceStart: unknown;
  sourceEnd: unknown;
  fallbackDuration: unknown;
}): number {
  const source = videoOverlaySourceWindow(assetDuration, sourceStart, sourceEnd, fallbackDuration);
  const elapsed = Math.max(0, finiteOr(timelineTime, 0) - finiteOr(timelineStart, 0))
    * normalizeVideoClipSpeed(speed);
  if (Boolean(loop)) return source.start + (elapsed % source.duration);
  return Math.min(Math.max(source.start, source.end - 0.001), source.start + elapsed);
}

export function normalizeVideoClipFade(value: unknown, timelineDuration: number): number {
  const safeDuration = Math.max(0, finiteOr(timelineDuration, 0));
  return Math.min(
    MAX_VIDEO_CLIP_FADE_SECONDS,
    safeDuration / 2,
    Math.max(0, finiteOr(value, 0)),
  );
}

export function videoClipEdgeFadeOpacity(
  timelineOffset: number,
  timelineDuration: number,
  fadeIn: unknown,
  fadeOut: unknown,
): number {
  const safeDuration = Math.max(0, finiteOr(timelineDuration, 0));
  if (safeDuration <= 0) return 1;
  const safeOffset = Math.min(safeDuration, Math.max(0, finiteOr(timelineOffset, 0)));
  const safeFadeIn = normalizeVideoClipFade(fadeIn, safeDuration);
  const safeFadeOut = normalizeVideoClipFade(fadeOut, safeDuration);
  const fadeInOpacity = safeFadeIn > 0 ? safeOffset / safeFadeIn : 1;
  const fadeOutOpacity = safeFadeOut > 0 ? (safeDuration - safeOffset) / safeFadeOut : 1;
  return Math.min(1, Math.max(0, Math.min(fadeInOpacity, fadeOutOpacity)));
}
