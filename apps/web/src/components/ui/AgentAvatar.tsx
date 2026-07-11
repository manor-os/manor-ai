import { useState, type CSSProperties } from "react";
import { MANOR_AGENT_ID, MANOR_AGENT_NAME, MANOR_AGENT_TYPE, isMasterAgent } from "../../lib/constants";
import ManorAvatar from "./ManorAvatar";

/**
 * AgentAvatar — generates a deterministic, on-brand avatar for an agent from
 * its persona (name + optional role/seed). Falls back to a real image when
 * `avatarUrl` is provided and points to a user-uploaded image.
 *
 * The generated look is a **cute hand-drawn character face** (可爱简笔画):
 * big shiny eyes, rosy cheeks and a sweet smile, in ink strokes on a soft
 * low-saturation tint — composed deterministically so every agent gets its
 * own little character, in the platform's calm palette.
 */

// Soft low-saturation background tints (light end of the palette).
const TINTS = [
  "#e5eeeb", "#e3e9f1", "#ece9f5", "#dceae3",
  "#f3ecd6", "#f3e5ed", "#e8eff4", "#efedea",
  "#eef2ee", "#f0eeee", "#e9f0ef", "#f2efe8",
  "#edf0f4", "#f0ecf3", "#f4eeee", "#eaf0e8",
];
const FACE_FILLS = ["#fffdfa", "#fffaf5", "#fff8ef", "#fdfbf8", "#fff6f2", "#fbfaf6"];
const INKS = ["#2b2724", "#34302c", "#28312f", "#312b34"];
const BLUSHES = [
  "rgba(228,138,128,0.5)",
  "rgba(218,128,148,0.42)",
  "rgba(216,151,115,0.42)",
  "rgba(194,137,126,0.36)",
];

const HEAD_VARIANTS = 6;
const EYE_VARIANTS = 8;
const MOUTH_VARIANTS = 8;
const TOP_VARIANTS = 12;
const ACCESSORY_VARIANTS = 8;
const NOSE_VARIANTS = 4;
const CHEEK_VARIANTS = 4;
const LAYOUT_VARIANTS = 4;

export const GENERATED_AGENT_AVATAR_VARIANTS =
  TINTS.length *
  FACE_FILLS.length *
  INKS.length *
  HEAD_VARIANTS *
  EYE_VARIANTS *
  MOUTH_VARIANTS *
  TOP_VARIANTS *
  ACCESSORY_VARIANTS *
  NOSE_VARIANTS *
  CHEEK_VARIANTS *
  LAYOUT_VARIANTS;

export function isUserUploadedAgentAvatarUrl(avatarUrl?: string | null): boolean {
  const src = (avatarUrl || "").trim();
  if (!src) return false;
  if (src.startsWith("data:image/") || src.startsWith("blob:")) return true;
  if (src.startsWith("/api/v1/fs/") && src.includes("/avatars/")) return true;
  try {
    const parsed = new URL(src, "http://manor.local");
    return parsed.pathname.startsWith("/api/v1/fs/") && parsed.pathname.includes("/avatars/");
  } catch {
    return false;
  }
}

function hashString(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

/** Deterministic initials (1–2 chars) — used by other surfaces if needed. */
export function agentInitials(name: string): string {
  const words = (name || "").trim().split(/[\s_-]+/).filter(Boolean);
  if (words.length === 0) return "?";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}

/** Soft tint background for an agent, seeded by persona. */
export function agentTint(name: string, seed = ""): string {
  return TINTS[hashString(`${name}::${seed}`) % TINTS.length];
}

function isManorAgentAvatar(name: string, seed = ""): boolean {
  const normalizedName = (name || "").trim().toLowerCase();
  const normalizedSeed = (seed || "").trim().toLowerCase();
  return (
    isMasterAgent(normalizedSeed) ||
    normalizedSeed === MANOR_AGENT_TYPE ||
    normalizedSeed.includes(MANOR_AGENT_ID) ||
    normalizedName === MANOR_AGENT_NAME.toLowerCase() ||
    normalizedName === "manor master agent" ||
    normalizedName === "manor 主智能体" ||
    normalizedName === "agente maestro de manor"
  );
}

interface AgentAvatarProps {
  name: string;
  avatarUrl?: string | null;
  /** Extra persona signal (role / category / id) to diversify generation. */
  seed?: string;
  size?: number;
  /** Avatar outline shape. */
  shape?: "circle" | "rounded";
  className?: string;
  style?: CSSProperties;
}

export default function AgentAvatar({
  name, avatarUrl, seed = "", size = 40, shape = "circle", className = "", style,
}: AgentAvatarProps) {
  const [broken, setBroken] = useState<string | null>(null);
  const radius = shape === "circle" ? "50%" : Math.round(size * 0.28);
  const src = isUserUploadedAgentAvatarUrl(avatarUrl) ? (avatarUrl || "").trim() : "";

  if (isManorAgentAvatar(name, seed)) {
    return (
      <ManorAvatar
        size={size}
        shape={shape}
        className={className}
        style={style}
      />
    );
  }

  if (src && broken !== src) {
    return (
      <img
        src={src}
        alt={name}
        className={className}
        onError={() => setBroken(src)}
        style={{
          width: size, height: size, minWidth: size, minHeight: size,
          objectFit: "cover", display: "block", borderRadius: radius,
          ...style,
        }}
      />
    );
  }

  const seedKey = `${name}::${seed}`;
  const h = hashString(seedKey);
  return (
    <span
      className={className}
      aria-label={name}
      style={{
        display: "block",
        width: size, height: size, minWidth: size, minHeight: size,
        borderRadius: radius,
        overflow: "hidden",
        background: TINTS[h % TINTS.length],
        boxShadow: "inset 0 1px 1px rgba(255,255,255,0.45), inset 0 -3px 6px rgba(0,0,0,0.05)",
        flexShrink: 0,
        userSelect: "none",
        ...style,
      }}
    >
      <AgentFace seedKey={seedKey} size={size} />
    </span>
  );
}

/** Deterministic cute character face. ViewBox is 100×100. */
function AgentFace({ seedKey, size }: { seedKey: string; size: number }) {
  const pick = (salt: string, n: number) => hashString(`${seedKey}::${salt}`) % n;
  const head = pick("head", HEAD_VARIANTS);
  const eyes = pick("eyes", EYE_VARIANTS);
  const mouth = pick("mouth", MOUTH_VARIANTS);
  const top = pick("top", TOP_VARIANTS);
  const accessory = pick("accessory", ACCESSORY_VARIANTS);
  const nose = pick("nose", NOSE_VARIANTS);
  const cheek = pick("cheek", CHEEK_VARIANTS);
  const layout = pick("layout", LAYOUT_VARIANTS);
  const faceFill = FACE_FILLS[pick("face-fill", FACE_FILLS.length)];
  const ink = INKS[pick("ink", INKS.length)];
  const blush = BLUSHES[cheek];
  const sw = 3;
  const stroke = {
    stroke: ink, strokeWidth: sw, fill: "none",
    strokeLinecap: "round" as const, strokeLinejoin: "round" as const,
  };
  // Wide-set, low eyes + big head read as "cute / baby-faced".
  const eyeLayouts = [
    { ex: 36, ex2: 64, ey: 55, er: 5.6 },
    { ex: 35, ex2: 65, ey: 54, er: 5.4 },
    { ex: 37, ex2: 63, ey: 56, er: 5.2 },
    { ex: 34, ex2: 66, ey: 55, er: 5.0 },
  ];
  const { ex, ex2, ey, er } = eyeLayouts[layout];

  // Big shiny eye (ink iris + white highlight). Used by default look.
  const shinyEye = (cx: number) => (
    <g key={cx}>
      <circle cx={cx} cy={ey} r={er} fill={ink} />
      <circle cx={cx - er * 0.35} cy={ey - er * 0.4} r={er * 0.32} fill="#fff" />
    </g>
  );

  return (
    <svg viewBox="0 0 100 100" width={size} height={size} style={{ display: "block" }} aria-hidden>
      {/* head — big & round for cuteness */}
      {head === 0 && <circle cx={50} cy={54} r={29} {...stroke} fill={faceFill} />}
      {head === 1 && <rect x={22} y={26} width={56} height={57} rx={24} {...stroke} fill={faceFill} />}
      {head === 2 && <ellipse cx={50} cy={55} rx={28} ry={30} {...stroke} fill={faceFill} />}
      {head === 3 && <path d="M24 52 a26 26 0 0 1 52 0 v4 a26 28 0 0 1 -52 0 z" {...stroke} fill={faceFill} />}
      {head === 4 && <path d="M50 24 q23 0 28 25 q2 23 -28 35 q-30 -12 -28 -35 q5 -25 28 -25z" {...stroke} fill={faceFill} />}
      {head === 5 && <rect x={21} y={25} width={58} height={58} rx={18} {...stroke} fill={faceFill} />}

      {/* hair / headgear */}
      {top === 1 && <path d="M25 40 q25 -24 50 0" {...stroke} />}
      {top === 2 && (<><path d="M28 36 q9 -13 19 -6" {...stroke} /><path d="M47 28 q11 -8 22 7" {...stroke} /></>)}
      {top === 3 && <path d="M23 42 q27 -28 54 0 q-27 -11 -54 0 z" fill={ink} stroke={ink} strokeWidth={1} />}
      {top === 4 && (<>{/* headphones */}<path d="M25 54 a25 25 0 0 1 50 0" {...stroke} /><rect x={17} y={50} width={10} height={17} rx={5} fill={ink} /><rect x={73} y={50} width={10} height={17} rx={5} fill={ink} /></>)}
      {top === 5 && (<>{/* antenna w/ dot */}<line x1={50} y1={26} x2={50} y2={15} {...stroke} /><circle cx={50} cy={12} r={4.5} fill={ink} /></>)}
      {top === 6 && (<>{/* little tuft */}<path d="M44 27 q6 -9 12 0" {...stroke} /></>)}
      {top === 7 && <path d="M27 37 q23 -17 46 0 l-5 -9 h-36z" fill={ink} opacity={0.92} />}
      {top === 8 && <path d="M27 39 q16 -19 33 -10 q-8 7 -14 20 q-9 -8 -19 -10z" fill={ink} opacity={0.92} />}
      {top === 9 && (<><path d="M33 31 q7 -7 14 0" {...stroke} /><path d="M53 31 q7 -7 14 0" {...stroke} /></>)}
      {top === 10 && <path d="M31 36 q19 -21 38 0" {...stroke} strokeDasharray="4 5" />}
      {top === 11 && (<><circle cx={32} cy={34} r={4} fill={ink} /><circle cx={68} cy={34} r={4} fill={ink} /></>)}

      {/* accessories */}
      {accessory === 1 && (
        <>
          <circle cx={ex} cy={ey} r={9} {...stroke} />
          <circle cx={ex2} cy={ey} r={9} {...stroke} />
          <line x1={ex + 9} y1={ey} x2={ex2 - 9} y2={ey} {...stroke} />
        </>
      )}
      {accessory === 2 && (
        <>
          <rect x={ex - 9} y={ey - 8} width={18} height={16} rx={5} {...stroke} />
          <rect x={ex2 - 9} y={ey - 8} width={18} height={16} rx={5} {...stroke} />
          <line x1={ex + 9} y1={ey} x2={ex2 - 9} y2={ey} {...stroke} />
        </>
      )}
      {accessory === 3 && (<><path d={`M${ex - 8} ${ey - 11} q8 -4 16 0`} {...stroke} /><path d={`M${ex2 - 8} ${ey - 11} q8 -4 16 0`} {...stroke} /></>)}
      {accessory === 4 && (<><circle cx={30} cy={63} r={1.4} fill={ink} opacity={0.45} /><circle cx={34} cy={66} r={1.1} fill={ink} opacity={0.35} /><circle cx={70} cy={63} r={1.4} fill={ink} opacity={0.45} /><circle cx={66} cy={66} r={1.1} fill={ink} opacity={0.35} /></>)}
      {accessory === 5 && <path d="M70 42 l2 4 4 1 -4 1 -2 4 -2 -4 -4 -1 4 -1z" fill={ink} opacity={0.55} />}
      {accessory === 6 && (<><line x1={25} y1={64} x2={34} y2={62} {...stroke} strokeWidth={1.6} /><line x1={66} y1={62} x2={75} y2={64} {...stroke} strokeWidth={1.6} /></>)}
      {accessory === 7 && <path d="M62 36 q6 -5 11 0 q-5 4 -11 0z" fill={ink} opacity={0.55} />}

      {/* blush cheeks */}
      <ellipse cx={ex - 4} cy={ey + 9} rx={cheek === 2 ? 6.8 : 5.5} ry={cheek === 1 ? 2.7 : 3.4} fill={blush} />
      <ellipse cx={ex2 + 4} cy={ey + 9} rx={cheek === 2 ? 6.8 : 5.5} ry={cheek === 1 ? 2.7 : 3.4} fill={blush} />

      {/* eyes */}
      {eyes === 0 && (<>{shinyEye(ex)}{shinyEye(ex2)}</>)}
      {eyes === 1 && (<>{/* happy ^ ^ */}<path d={`M${ex - 6} ${ey + 2} q6 -8 12 0`} {...stroke} /><path d={`M${ex2 - 6} ${ey + 2} q6 -8 12 0`} {...stroke} /></>)}
      {eyes === 2 && (<>{/* sparkle */}<circle cx={ex} cy={ey} r={er} fill={ink} /><circle cx={ex2} cy={ey} r={er} fill={ink} /><circle cx={ex - 2} cy={ey - 2} r={1.5} fill="#fff" /><circle cx={ex2 - 2} cy={ey - 2} r={1.5} fill="#fff" /><circle cx={ex + 2.4} cy={ey + 2} r={0.9} fill="#fff" /><circle cx={ex2 + 2.4} cy={ey + 2} r={0.9} fill="#fff" /></>)}
      {eyes === 3 && (<>{/* wink */}{shinyEye(ex)}<path d={`M${ex2 - 6} ${ey + 1} q6 -8 12 0`} {...stroke} /></>)}
      {eyes === 4 && (<>{/* round outline + pupil */}<circle cx={ex} cy={ey} r={6} {...stroke} /><circle cx={ex2} cy={ey} r={6} {...stroke} /><circle cx={ex} cy={ey + 1} r={2.4} fill={ink} /><circle cx={ex2} cy={ey + 1} r={2.4} fill={ink} /></>)}
      {eyes === 5 && (<><path d={`M${ex - 6} ${ey} q6 5 12 0`} {...stroke} /><path d={`M${ex2 - 6} ${ey} q6 5 12 0`} {...stroke} /></>)}
      {eyes === 6 && (<><circle cx={ex} cy={ey} r={3.6} fill={ink} /><circle cx={ex2} cy={ey} r={3.6} fill={ink} /><line x1={ex - 7} y1={ey - 4} x2={ex - 10} y2={ey - 7} {...stroke} strokeWidth={1.7} /><line x1={ex2 + 7} y1={ey - 4} x2={ex2 + 10} y2={ey - 7} {...stroke} strokeWidth={1.7} /></>)}
      {eyes === 7 && (<>{shinyEye(ex)}<circle cx={ex2} cy={ey} r={5.5} {...stroke} /><circle cx={ex2} cy={ey + 1} r={2.2} fill={ink} /></>)}

      {/* nose */}
      {nose === 1 && <path d="M50 60 q-2 3 1 5" {...stroke} strokeWidth={1.5} />}
      {nose === 2 && <circle cx={50} cy={63} r={1.6} fill={ink} opacity={0.38} />}
      {nose === 3 && <path d="M48 63 h4" {...stroke} strokeWidth={1.6} />}

      {/* mouth — small & sweet */}
      {mouth === 0 && <path d="M44 70 q6 6 12 0" {...stroke} />}
      {mouth === 1 && (<>{/* cat ω */}<path d="M45 69 q2.5 4 5 0 q2.5 4 5 0" {...stroke} /></>)}
      {mouth === 2 && <path d="M45 69 q5 7 10 0 z" fill={ink} stroke={ink} strokeWidth={1} />}
      {mouth === 3 && <circle cx={50} cy={70} r={2.8} {...stroke} />}
      {mouth === 4 && <path d="M43 68 q7 9 14 0" {...stroke} />}
      {mouth === 5 && <path d="M45 70 h10" {...stroke} />}
      {mouth === 6 && (<><path d="M43 69 q7 8 14 0" {...stroke} /><path d="M49 72 q2 3 4 0" stroke="#df8a83" strokeWidth={1.4} fill="none" strokeLinecap="round" /></>)}
      {mouth === 7 && <path d="M46 70 q5 4 10 0" {...stroke} strokeWidth={2.4} />}
    </svg>
  );
}
