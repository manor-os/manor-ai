/**
 * Gradient initial-letter avatar.
 *
 * Deterministic colour from a hash of `name` — same name always gets the
 * same gradient. Shared across Team, Users, Agents, Workspace pages.
 *
 * If `src` is provided but the image 404s (stale `users.avatar_url`
 * pointing at a deleted file on disk — common in dev after FS resets),
 * we fall through to the initials circle instead of rendering the
 * browser's broken-image icon.
 */
import { useState } from "react";

const AVATAR_GRADIENTS = [
  { from: "#e5eeeb", to: "#ccded9", fg: "#436b65" },
  { from: "#e3e9f1", to: "#bfdbfe", fg: "#3f57a0" },
  { from: "#f3e5ed", to: "#fbcfe8", fg: "#be185d" },
  { from: "#ece9f5", to: "#ddd6fe", fg: "#6443a0" },
  { from: "#f3ecd6", to: "#ecdca4", fg: "#936027" },
  { from: "#dceae3", to: "#c4dfd2", fg: "#3f7361" },
  { from: "#e8eff4", to: "#bae6fd", fg: "#426c87" },
  { from: "#f1dddb", to: "#ecc8c5", fg: "#a23e38" },
];

export function getAvatarColor(name: string) {
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash);
  return AVATAR_GRADIENTS[Math.abs(hash) % AVATAR_GRADIENTS.length];
}

interface AvatarProps {
  name: string;
  size?: number;
  src?: string;          // optional image URL; falls back to initials
  className?: string;
}

const brokenAvatarUrls = new Set<string>();

export default function Avatar({ name, size = 32, src, className }: AvatarProps) {
  const c = getAvatarColor(name);
  const imageSrc = (src || "").trim();
  const [brokenSrc, setBrokenSrc] = useState<string | null>(() => (imageSrc && brokenAvatarUrls.has(imageSrc) ? imageSrc : null));
  const isBroken = !!imageSrc && (brokenSrc === imageSrc || brokenAvatarUrls.has(imageSrc));
  if (imageSrc && !isBroken) {
    return (
      <img
        src={imageSrc}
        alt={name}
        className={className}
        onError={() => {
          brokenAvatarUrls.add(imageSrc);
          setBrokenSrc(imageSrc);
        }}
        style={{
          width: size, height: size, minWidth: size, minHeight: size,
          borderRadius: "50%", objectFit: "cover", flexShrink: 0,
        }}
      />
    );
  }
  return (
    <span
      className={className}
      style={{
        display: "flex", alignItems: "center", justifyContent: "center",
        width: size, height: size, minWidth: size, minHeight: size,
        borderRadius: "50%",
        background: `linear-gradient(135deg, ${c.from}, ${c.to})`,
        color: c.fg, fontSize: size * 0.38, fontWeight: 800,
        userSelect: "none", flexShrink: 0,
      }}
    >
      {(name || "?").charAt(0).toUpperCase()}
    </span>
  );
}
