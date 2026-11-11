/**
 * "Mochi" AI employee avatars: a soft rounded body rising from the bottom of
 * the circle, with ears, eyes, a mouth and an accessory picked from small sets.
 *
 * 10 tops × 10 accessories × 6 eyes × 5 mouths = 3000 distinct line drawings,
 * plus 6 body tints and 16 backgrounds. An index (0…2999) maps to traits
 * through a stride coprime with 3000 and one more one-to-one mix, so
 * consecutive indexes look very different and the first 3000 never repeat.
 * Without an index, the seed is hashed to one.
 */

export const MOCHI_TOTAL = 3000;

export const MOCHI_BACKGROUNDS = [
  "#e5eeeb", "#e3e9f1", "#ece9f5", "#dceae3",
  "#f3ecd6", "#f3e5ed", "#e8eff4", "#efedea",
  "#eef2ee", "#f0eeee", "#e9f0ef", "#f2efe8",
  "#edf0f4", "#f0ecf3", "#f4eeee", "#eaf0e8",
];

const BODIES = ["#fffaf3", "#fff1ec", "#f1f8f1", "#f5f1fd", "#eef5fd", "#fff8dc"];
const INK = "#2b2724";
const BLUSH = "rgba(236,140,135,0.42)";

export function hashSeed(seed: string): number {
  let h = 2166136261;
  for (let i = 0; i < seed.length; i++) {
    h ^= seed.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

export interface MochiTraits {
  ears: number;
  acc: number;
  eyes: number;
  mouth: number;
  body: number;
  background: number;
}

export function mochiTraits(index: number): MochiTraits {
  const idx = ((Math.floor(index) % MOCHI_TOTAL) + MOCHI_TOTAL) % MOCHI_TOTAL;
  const c = (idx * 1783 + 7) % MOCHI_TOTAL;
  const d0 = c % 10;
  const d1 = Math.floor(c / 10) % 10;
  const d2 = Math.floor(c / 100) % 6;
  const d3 = Math.floor(c / 600) % 5;
  const ears = (d0 + d1 + d2) % 10;
  return {
    ears,
    acc: (d1 + 7 * ears + d3) % 10,
    eyes: d2,
    mouth: d3,
    body: (idx * 5 + 1) % BODIES.length,
    background: (idx * 7 + 3) % MOCHI_BACKGROUNDS.length,
  };
}

export function mochiIndexForSeed(seed: string): number {
  return hashSeed(`${seed}::mochi`) % MOCHI_TOTAL;
}

/** SVG markup (viewBox 0 0 100 100) for the body; the caller paints the background. */
export function mochiSvg(traits: MochiTraits): string {
  const body = BODIES[traits.body];
  const line = `stroke="${INK}" stroke-width="2.4" stroke-linejoin="round"`;
  const thin = `stroke="${INK}" stroke-width="2.2" fill="none" stroke-linecap="round"`;
  const capOn = traits.acc === 8;
  const ears = [
    "",
    `<path d="M20 50 L24 22 L44 36 Z" fill="${body}" ${line}/><path d="M80 50 L76 22 L56 36 Z" fill="${body}" ${line}/><path d="M25 41 L27 29 L37 36 Z" fill="${BLUSH}"/><path d="M75 41 L73 29 L63 36 Z" fill="${BLUSH}"/>`,
    `<ellipse cx="38" cy="22" rx="7.5" ry="15" fill="${body}" ${line}/><ellipse cx="62" cy="22" rx="7.5" ry="15" fill="${body}" ${line}/><ellipse cx="38" cy="23" rx="3.2" ry="9" fill="${BLUSH}"/><ellipse cx="62" cy="23" rx="3.2" ry="9" fill="${BLUSH}"/>`,
    `<circle cx="26" cy="40" r="10" fill="${body}" ${line}/><circle cx="74" cy="40" r="10" fill="${body}" ${line}/>`,
    `<path d="M50 33 q-1 -7 0 -11" ${thin}/><path d="M50 23 q-10 -9 -16 -2 q7 7 16 2z" fill="#a9d1a2" stroke="${INK}" stroke-width="2" stroke-linejoin="round"/><path d="M50 23 q10 -9 16 -2 q-7 7 -16 2z" fill="#a9d1a2" stroke="${INK}" stroke-width="2" stroke-linejoin="round"/>`,
    `<path d="M50 33 V21" ${thin}/><circle cx="50" cy="18" r="4.2" fill="#f2c94c" stroke="${INK}" stroke-width="2"/>`,
    `<path d="M24 48 L20 12 L44 32 Z" fill="${body}" ${line}/><path d="M76 48 L80 12 L56 32 Z" fill="${body}" ${line}/><path d="M26 38 L24 22 L36 32 Z" fill="#fbe1cf"/><path d="M74 38 L76 22 L64 32 Z" fill="#fbe1cf"/>`,
    `<circle cx="22" cy="38" r="13" fill="${body}" ${line}/><circle cx="78" cy="38" r="13" fill="${body}" ${line}/><circle cx="22" cy="38" r="7" fill="${BLUSH}"/><circle cx="78" cy="38" r="7" fill="${BLUSH}"/>`,
    `<path d="M45 33 q-3 -11 6 -12 q8 -1 6 7 q-2 5 -7 2" ${thin}/>`,
    `<path d="M45 33 l-3 -9 M50 32 v-10 M55 33 l3 -9" ${thin}/>`,
  ][traits.ears];
  const ey = 60;
  const l = 40;
  const r = 60;
  const dot = (cx: number, radius: number) =>
    `<circle cx="${cx}" cy="${ey}" r="${radius}" fill="${INK}"/><circle cx="${cx - radius * 0.32}" cy="${ey - radius * 0.38}" r="${radius * 0.34}" fill="#fff"/>`;
  const happy = (cx: number) =>
    `<path d="M${cx - 4.6} ${ey + 1.5} q4.6 -6 9.2 0" stroke="${INK}" stroke-width="3" fill="none" stroke-linecap="round"/>`;
  const sleepy = (cx: number) =>
    `<path d="M${cx - 4.6} ${ey - 1} q4.6 4.6 9.2 0" stroke="${INK}" stroke-width="3" fill="none" stroke-linecap="round"/>`;
  const sparkle = (cx: number) =>
    `<circle cx="${cx}" cy="${ey}" r="5" fill="${INK}"/><circle cx="${cx - 1.8}" cy="${ey - 1.9}" r="1.7" fill="#fff"/><circle cx="${cx + 1.7}" cy="${ey + 1.6}" r="0.9" fill="#fff"/>`;
  const eyes = [
    dot(l, 4.4) + dot(r, 4.4),
    happy(l) + happy(r),
    dot(l, 5.2) + dot(r, 5.2),
    dot(l, 4.4) + happy(r),
    sparkle(l) + sparkle(r),
    sleepy(l) + sleepy(r),
  ][traits.eyes];
  const mouth = [
    `<path d="M46.5 68 q3.5 4 7 0" stroke="${INK}" stroke-width="2.6" fill="none" stroke-linecap="round"/>`,
    `<path d="M44 67.5 q3 4 6 0 q3 4 6 0" stroke="${INK}" stroke-width="2.6" fill="none" stroke-linecap="round"/>`,
    `<ellipse cx="50" cy="69.5" rx="2.8" ry="3.2" fill="${INK}"/>`,
    `<path d="M45 67 q5 7 10 0 z" fill="${INK}"/><path d="M47.6 70 q2.4 2 4.8 0" stroke="#e88f8a" stroke-width="1.8" fill="none" stroke-linecap="round"/>`,
    `<path d="M43.5 67 q6.5 7 13 0" stroke="${INK}" stroke-width="2.6" fill="none" stroke-linecap="round"/>`,
  ][traits.mouth];
  const blush = `<ellipse cx="29" cy="68" rx="7" ry="4.2" fill="${BLUSH}"/><ellipse cx="71" cy="68" rx="7" ry="4.2" fill="${BLUSH}"/>`;
  const petals = [0, 72, 144, 216, 288]
    .map((deg) => {
      const rad = (deg * Math.PI) / 180;
      return `<circle cx="${(67 + 4.2 * Math.cos(rad)).toFixed(2)}" cy="${(40 + 4.2 * Math.sin(rad)).toFixed(2)}" r="3.4" fill="#ffffff" stroke="${INK}" stroke-width="1.6"/>`;
    })
    .join("");
  const acc = [
    "",
    `<circle cx="${l}" cy="${ey}" r="8" stroke="${INK}" stroke-width="2.2" fill="none"/><circle cx="${r}" cy="${ey}" r="8" stroke="${INK}" stroke-width="2.2" fill="none"/><path d="M${l + 8} ${ey} h${r - l - 16}" stroke="${INK}" stroke-width="2.2"/>`,
    `<path d="M15 66 C15 30 85 30 85 66" stroke="${INK}" stroke-width="3.2" fill="none" stroke-linecap="round"/><rect x="8" y="56" width="12" height="20" rx="6" fill="${INK}"/><rect x="80" y="56" width="12" height="20" rx="6" fill="${INK}"/>`,
    `<path d="M66 40 l10 -6 l-1 12 z M66 40 l-10 -6 l1 12 z" fill="#f4b3be" stroke="${INK}" stroke-width="2" stroke-linejoin="round"/><circle cx="66" cy="40" r="2.6" fill="#f4b3be" stroke="${INK}" stroke-width="2"/>`,
    `<path d="M10 79 Q50 91 90 79 L90 88 Q50 100 10 88 Z" fill="#cfe0f2" stroke="${INK}" stroke-width="2.2" stroke-linejoin="round"/><path d="M64 88 l3 9 l7 -2 l-3 -8" fill="#cfe0f2" stroke="${INK}" stroke-width="2.2" stroke-linejoin="round"/>`,
    `<path d="M50 84 l-9 -5 v10 z M50 84 l9 -5 v10 z" fill="#f4b3be" stroke="${INK}" stroke-width="2" stroke-linejoin="round"/><circle cx="50" cy="84" r="2.4" fill="#f4b3be" stroke="${INK}" stroke-width="2"/>`,
    `<path d="M32 38 l2.4 4.8 5.3 0.8 -3.8 3.7 0.9 5.3 -4.8 -2.5 -4.8 2.5 0.9 -5.3 -3.8 -3.7 5.3 -0.8z" fill="#f2c94c" stroke="${INK}" stroke-width="1.8" stroke-linejoin="round"/>`,
    `${petals}<circle cx="67" cy="40" r="2.6" fill="#f2c94c"/>`,
    `<path d="M23 50 C24 27 76 27 77 50 Z" fill="#cfe0f2" ${line}/><path d="M62 49 H88 Q90 54 84 55 H60 Z" fill="#cfe0f2" stroke="${INK}" stroke-width="2.2" stroke-linejoin="round"/><circle cx="50" cy="29" r="2.4" fill="${INK}"/>`,
    `<path d="M27 47 q-6 -4 -4 -10" ${thin}/><path d="M73 47 q6 -4 4 -10" ${thin}/>`,
  ][traits.acc];
  // Under a cap, tops that rise from the middle of the head draw over it.
  const poke = capOn && [2, 4, 5, 8, 9].includes(traits.ears) ? ears : "";
  return (
    ears +
    `<path d="M6 110 C6 56 24 32 50 32 C76 32 94 56 94 110 Z" fill="${body}" ${line}/>` +
    `<ellipse cx="36" cy="44" rx="10" ry="5.5" fill="#ffffff" opacity="0.75" transform="rotate(-18 36 44)"/>` +
    blush + eyes + mouth + acc + poke
  );
}

/** The drawing as an `<img>` source, so callers never inject markup. */
export function mochiDataUri(traits: MochiTraits): string {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">${mochiSvg(traits)}</svg>`;
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}
