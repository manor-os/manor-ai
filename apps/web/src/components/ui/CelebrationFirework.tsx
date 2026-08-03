import type { CSSProperties } from "react";

type FireworkTone = "gold" | "sage" | "rose" | "stone";

type FireworkParticle = {
  angle: number;
  depth: number;
  distance: number;
  length: number;
  tone: FireworkTone;
};

type FireworkParticleStyle = CSSProperties & Record<`--firework-${string}`, string>;

const PARTICLE_COUNT = 20;
const PARTICLE_TONES: FireworkTone[] = ["gold", "sage", "stone", "rose"];

const PARTICLES: FireworkParticle[] = Array.from({ length: PARTICLE_COUNT }, (_, index) => ({
  angle: -90 + (360 / PARTICLE_COUNT) * index,
  depth: ((index * 17) % 31) - 15,
  distance: 26 + (index % 4) * 3,
  length: 6 + (index % 3) * 2,
  tone: PARTICLE_TONES[index % PARTICLE_TONES.length],
}));

function particleStyle(particle: FireworkParticle, index: number): FireworkParticleStyle {
  const radians = (particle.angle * Math.PI) / 180;
  const x = Math.cos(radians) * particle.distance;
  const y = Math.sin(radians) * particle.distance;
  const xEnd = Math.cos(radians) * (particle.distance + 9);
  const yEnd = Math.sin(radians) * (particle.distance + 9) + 8;

  return {
    "--firework-angle": `${particle.angle + 90}deg`,
    "--firework-delay": `${(index % 5) * 22}ms`,
    "--firework-depth": `${particle.depth}px`,
    "--firework-depth-end": `${Math.round(particle.depth * 1.18)}px`,
    "--firework-length": `${particle.length}px`,
    "--firework-tone": `var(--firework-${particle.tone})`,
    "--firework-x": `${x.toFixed(1)}px`,
    "--firework-x-end": `${xEnd.toFixed(1)}px`,
    "--firework-y": `${y.toFixed(1)}px`,
    "--firework-y-end": `${yEnd.toFixed(1)}px`,
  };
}

/** Decorative, lightweight 3D celebration used after a successful creation flow. */
export default function CelebrationFirework() {
  return (
    <div className="celebration-firework" aria-hidden="true">
      <div className="celebration-firework__scene">
        <span className="celebration-firework__rocket" />
        <span className="celebration-firework__glow" />
        <span className="celebration-firework__shockwave" />
        <span className="celebration-firework__core" />
        {PARTICLES.map((particle, index) => (
          <span
            key={`${particle.angle}-${particle.depth}`}
            className="celebration-firework__particle"
            style={particleStyle(particle, index)}
          />
        ))}
      </div>
    </div>
  );
}
