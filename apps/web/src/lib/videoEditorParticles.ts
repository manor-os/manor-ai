export const PARTICLE_SHAPES = ["dot", "confetti", "spark"] as const;
export type ParticleShape = (typeof PARTICLE_SHAPES)[number];

export const PARTICLE_MOTIONS = ["burst", "drift", "orbit"] as const;
export type ParticleMotion = (typeof PARTICLE_MOTIONS)[number];

export type ParticleLayerConfig = {
  particleCount?: number;
  particleShape?: ParticleShape;
  particleMotion?: ParticleMotion;
  particleSeed?: number;
  particleSize?: number;
  particleSpeed?: number;
  particleGravity?: number;
  particleSpread?: number;
  particleLoop?: boolean;
  fill?: string;
  fillSecondary?: string;
  stroke?: string;
  strokeWidth?: number;
};

export type NormalizedParticleLayer = Required<Omit<ParticleLayerConfig, "fillSecondary">> & {
  fillSecondary: string;
};

export type ParticleFrame = {
  x: number;
  y: number;
  rotation: number;
  opacity: number;
  size: number;
  color: string;
};

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, Number.isFinite(value) ? value : min));
}

function positiveModulo(value: number, divisor: number): number {
  return ((value % divisor) + divisor) % divisor;
}

// A stable integer hash keeps particle placement identical after seeking and export.
function seededUnit(seed: number, index: number, salt: number): number {
  let value = (Math.trunc(seed) ^ Math.imul(index + 1, 0x9e3779b1) ^ Math.imul(salt + 1, 0x85ebca6b)) >>> 0;
  value = Math.imul(value ^ (value >>> 16), 0x7feb352d);
  value = Math.imul(value ^ (value >>> 15), 0x846ca68b);
  return ((value ^ (value >>> 16)) >>> 0) / 4294967296;
}

export function normalizeParticleLayer(config: ParticleLayerConfig): NormalizedParticleLayer {
  const particleShape = PARTICLE_SHAPES.includes(config.particleShape as ParticleShape)
    ? config.particleShape as ParticleShape
    : "confetti";
  const particleMotion = PARTICLE_MOTIONS.includes(config.particleMotion as ParticleMotion)
    ? config.particleMotion as ParticleMotion
    : "burst";
  return {
    particleCount: Math.round(clamp(config.particleCount ?? 28, 6, 40)),
    particleShape,
    particleMotion,
    particleSeed: Math.round(clamp(config.particleSeed ?? 24, 0, 9999)),
    particleSize: clamp(config.particleSize ?? 1, 0.25, 3),
    particleSpeed: clamp(config.particleSpeed ?? 1, 0.25, 3),
    particleGravity: clamp(config.particleGravity ?? 0.72, -2, 3),
    particleSpread: clamp(config.particleSpread ?? 0.86, 0.1, 1.5),
    particleLoop: Boolean(config.particleLoop),
    fill: config.fill || "#f8c95c",
    fillSecondary: config.fillSecondary || "#65d6c4",
    stroke: config.stroke || "#ff6b7a",
    strokeWidth: clamp(config.strokeWidth ?? 0, 0, 20),
  };
}

function layerEnvelope(localTime: number, duration: number): number {
  if (localTime <= 0 || localTime >= duration) return 0;
  return Math.min(1, localTime / Math.min(0.12, duration / 4), (duration - localTime) / Math.min(0.18, duration / 4));
}

function particleColor(config: NormalizedParticleLayer, index: number): string {
  return [config.fill, config.fillSecondary, config.stroke][index % 3];
}

export function particleFrameAtTime(
  rawConfig: ParticleLayerConfig,
  index: number,
  localTime: number,
  duration: number,
  width: number,
  height: number,
): ParticleFrame {
  const config = normalizeParticleLayer(rawConfig);
  const safeDuration = Math.max(0.05, duration);
  const time = clamp(localTime, 0, safeDuration);
  const envelope = layerEnvelope(time, safeDuration);
  const seed = config.particleSeed;
  const unitA = seededUnit(seed, index, 0);
  const unitB = seededUnit(seed, index, 1);
  const unitC = seededUnit(seed, index, 2);
  const baseSize = Math.max(1.5, Math.min(width, height) * 0.025 * config.particleSize * (0.66 + unitC * 0.74));
  const color = particleColor(config, index);

  if (config.particleMotion === "drift") {
    const travelDuration = Math.max(0.65, 3.2 / config.particleSpeed);
    const phase = positiveModulo(time / travelDuration + unitB, 1);
    const sway = Math.sin((time * config.particleSpeed * 1.25 + unitC * 2) * Math.PI) * width * 0.07;
    return {
      x: (unitA - 0.5) * width * config.particleSpread + sway,
      y: height * (0.55 - phase * 1.1),
      rotation: (unitC * 2 - 1) * Math.PI + time * (unitA - 0.5) * config.particleSpeed * 2,
      opacity: envelope * Math.sin(Math.PI * phase) * (0.48 + unitC * 0.52),
      size: baseSize,
      color,
    };
  }

  if (config.particleMotion === "orbit") {
    const radius = Math.min(width, height) * config.particleSpread * (0.12 + unitB * 0.35);
    const angle = unitA * Math.PI * 2 + time * config.particleSpeed * (0.7 + unitC * 1.25) * (index % 2 ? -1 : 1);
    return {
      x: Math.cos(angle) * radius,
      y: Math.sin(angle) * radius * 0.58,
      rotation: angle + Math.PI / 2,
      opacity: envelope * (0.48 + unitB * 0.52),
      size: baseSize,
      color,
    };
  }

  const cycleDuration = Math.max(0.7, 1.7 / config.particleSpeed);
  const cycleTime = config.particleLoop ? positiveModulo(time, cycleDuration) : time;
  const delay = unitC * Math.min(0.16, cycleDuration * 0.12);
  const progress = clamp((cycleTime - delay) / Math.max(0.2, cycleDuration - delay), 0, 1);
  const angle = -Math.PI / 2 + (unitA - 0.5) * Math.PI * 2 * config.particleSpread;
  const velocity = Math.min(width, height) * (0.38 + unitB * 0.52);
  const easedDistance = velocity * (1 - Math.pow(1 - progress, 2));
  const gravity = config.particleGravity * height * 0.52 * progress * progress;
  const visible = cycleTime >= delay && progress < 1 ? Math.sin(Math.PI * progress) : 0;
  return {
    x: Math.cos(angle) * easedDistance,
    y: Math.sin(angle) * easedDistance + gravity,
    rotation: (unitB - 0.5) * Math.PI + progress * (2.5 + unitA * 4) * Math.PI,
    opacity: envelope * visible,
    size: baseSize * (0.75 + 0.4 * Math.sin(Math.PI * progress)),
    color,
  };
}

function drawParticleShape(
  ctx: CanvasRenderingContext2D,
  shape: ParticleShape,
  size: number,
): void {
  ctx.beginPath();
  if (shape === "dot") {
    ctx.arc(0, 0, size / 2, 0, Math.PI * 2);
  } else if (shape === "spark") {
    const outer = size * 0.68;
    const inner = size * 0.16;
    for (let point = 0; point < 8; point += 1) {
      const angle = -Math.PI / 2 + point * (Math.PI / 4);
      const radius = point % 2 === 0 ? outer : inner;
      const x = Math.cos(angle) * radius;
      const y = Math.sin(angle) * radius;
      if (point === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.closePath();
  } else {
    ctx.roundRect(-size * 0.48, -size * 0.22, size * 0.96, size * 0.44, Math.max(0.5, size * 0.12));
  }
}

export function drawParticleLayer(
  ctx: CanvasRenderingContext2D,
  rawConfig: ParticleLayerConfig,
  localTime: number,
  duration: number,
  width: number,
  height: number,
): void {
  const config = normalizeParticleLayer(rawConfig);
  for (let index = 0; index < config.particleCount; index += 1) {
    const frame = particleFrameAtTime(config, index, localTime, duration, width, height);
    if (frame.opacity <= 0.001) continue;
    ctx.save();
    ctx.translate(frame.x, frame.y);
    ctx.rotate(frame.rotation);
    ctx.globalAlpha *= frame.opacity;
    ctx.fillStyle = frame.color;
    if (config.particleShape === "spark") {
      ctx.shadowColor = frame.color;
      ctx.shadowBlur = frame.size * 1.4;
    }
    drawParticleShape(ctx, config.particleShape, frame.size);
    ctx.fill();
    if (config.strokeWidth > 0) {
      ctx.strokeStyle = "rgba(255,255,255,.72)";
      ctx.lineWidth = Math.min(frame.size * 0.18, config.strokeWidth);
      ctx.stroke();
    }
    ctx.restore();
  }
}
