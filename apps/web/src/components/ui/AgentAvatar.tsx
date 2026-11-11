import { useState, type CSSProperties } from "react";
import { MANOR_AGENT_ID, MANOR_AGENT_NAME, MANOR_AGENT_TYPE, isMasterAgent } from "../../lib/constants";
import ManorAvatar from "./ManorAvatar";

import { MOCHI_BACKGROUNDS, hashSeed, mochiIndexForSeed, mochiDataUri, mochiTraits } from "./mochiAvatar";

/**
 * AgentAvatar — a deterministic "mochi" character for each AI employee (see
 * mochiAvatar.ts), keyed by the employee's identity. Falls back to a real
 * image when `avatarUrl` points to a user-uploaded image.
 */

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

export function agentAvatarSeed(agent: {
  id?: unknown;
  category?: unknown;
  avatar_seed?: unknown;
  config?: unknown;
}): string {
  const config = agent.config && typeof agent.config === "object"
    ? agent.config as Record<string, unknown>
    : undefined;
  return String(
    agent.avatar_seed
      || config?.source_agent_id
      || agent.id
      || agent.category
      || "",
  );
}

const hashString = hashSeed;

/** Deterministic initials (1–2 chars) — used by other surfaces if needed. */
export function agentInitials(name: string): string {
  const words = (name || "").trim().split(/[\s_-]+/).filter(Boolean);
  if (words.length === 0) return "?";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}

/** Soft tint background for an agent, seeded by persona. */
export function agentTint(name: string, seed = ""): string {
  return MOCHI_BACKGROUNDS[mochiTraits(mochiIndexForSeed(seed || name)).background];
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

  // Keyed by the employee's identity, not its name, so renaming keeps the face.
  const traits = mochiTraits(mochiIndexForSeed(seed || name));
  return (
    <span
      className={className}
      aria-label={name}
      style={{
        display: "block",
        width: size, height: size, minWidth: size, minHeight: size,
        borderRadius: radius,
        overflow: "hidden",
        background: MOCHI_BACKGROUNDS[traits.background],
        boxShadow: "inset 0 1px 1px rgba(255,255,255,0.45), inset 0 -3px 6px rgba(0,0,0,0.05)",
        flexShrink: 0,
        userSelect: "none",
        ...style,
      }}
    >
      {/* An image, not injected markup: the drawing never touches the DOM as HTML. */}
      <img
        src={mochiDataUri(traits)}
        alt=""
        aria-hidden
        draggable={false}
        style={{ display: "block", width: "100%", height: "100%" }}
      />
    </span>
  );
}
