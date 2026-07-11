/**
 * UserAvatar — renders user, agent, or Manor AI avatar consistently.
 *
 * Usage:
 *   <UserAvatar name="John" avatarUrl="/img.jpg" size={28} />
 *   <UserAvatar type="agent" name="Research Agent" size={24} />
 *   <UserAvatar type="manor" size={32} />
 *   <UserAvatar type="workspace" size={32} />
 *
 * Resilience: if `avatarUrl` is set but the image 404s (common in dev
 * after FS resets, or when a user's stored avatar file is missing), we
 * fall through to the gradient-circle initials fallback instead of
 * rendering the browser's broken-image icon.
 */
import { useState } from "react";
import AgentAvatar from "./AgentAvatar";
import { IconUser, IconManorLogo, IconWorkspace, IconShield } from "../icons";

type AvatarType = "user" | "agent" | "manor" | "workspace" | "governance" | "none";

interface UserAvatarProps {
  name?: string | null;
  avatarUrl?: string | null;
  type?: AvatarType;
  seed?: string;
  size?: number;
  style?: React.CSSProperties;
}

const brokenAvatarUrls = new Set<string>();

export default function UserAvatar({ name, avatarUrl, type = "user", seed, size = 26, style }: UserAvatarProps) {
  const src = (avatarUrl || "").trim();
  const [brokenSrc, setBrokenSrc] = useState<string | null>(() => (src && brokenAvatarUrls.has(src) ? src : null));
  const isBroken = !!src && (brokenSrc === src || brokenAvatarUrls.has(src));

  // Manor AI — always the M logo
  if (type === "manor") {
    return (
      <div style={{
        width: size, height: size, borderRadius: "50%", flexShrink: 0,
        background: "linear-gradient(135deg, #5d7f77, #5f928a)",
        display: "flex", alignItems: "center", justifyContent: "center",
        border: "1.5px solid rgba(255,255,255,0.8)", ...style,
      }}>
        <IconManorLogo size={Math.round(size * 0.5)} style={{ color: "#fff" }} />
      </div>
    );
  }

  if (type === "agent") {
    return (
      <AgentAvatar
        name={name || ""}
        avatarUrl={src}
        seed={seed}
        size={size}
        shape="circle"
        style={{ border: "1.5px solid rgba(255,255,255,0.8)", ...style }}
      />
    );
  }

  // Governance / "Workspace rules" approvals — an ink shield, distinct from
  // both Manor AI (black "M") and a workspace entity (building).
  if (type === "governance") {
    return (
      <div style={{
        width: size, height: size, minWidth: size, minHeight: size,
        borderRadius: "50%", flexShrink: 0,
        background: "#33302c",
        display: "flex", alignItems: "center", justifyContent: "center",
        color: "#e7e5e4",
        border: "1.5px solid rgba(255,255,255,0.8)",
        ...style,
      }}>
        <IconShield size={Math.round(size * 0.5)} style={{ display: "block" }} />
      </div>
    );
  }

  if (type === "workspace") {
    return (
      <div style={{
        width: size, height: size, minWidth: size, minHeight: size,
        borderRadius: "50%", flexShrink: 0,
        background: "linear-gradient(135deg, rgba(244,248,247,0.98), rgba(255,255,255,0.98))",
        display: "flex", alignItems: "center", justifyContent: "center",
        color: "#4f7169",
        border: "1.5px solid rgba(79,113,105,0.18)",
        boxShadow: "inset 0 1px 0 rgba(255,255,255,0.86)",
        ...style,
      }}>
        <IconWorkspace size={Math.round(size * 0.52)} style={{ display: "block" }} />
      </div>
    );
  }

  // Actual avatar image
  if (src && !isBroken) {
    return (
      <img
        src={src}
        alt={name || ""}
        onError={() => {
          brokenAvatarUrls.add(src);
          setBrokenSrc(src);
        }}
        style={{
          width: size, height: size, borderRadius: "50%", flexShrink: 0,
          objectFit: "cover", border: "1.5px solid rgba(255,255,255,0.8)", ...style,
        }}
      />
    );
  }

  // Fallback: gradient circle with icon or initials
  return (
    <div style={{
      width: size, height: size, borderRadius: "50%", flexShrink: 0,
      background: type === "user" ? "linear-gradient(135deg, #e8eff4, #ddd6fe)"
        : "#f5f5f4",
      display: "flex", alignItems: "center", justifyContent: "center",
      color: "#78716c",
      fontSize: Math.round(size * 0.38), fontWeight: 700,
      border: "1.5px solid rgba(255,255,255,0.8)", ...style,
    }}>
      {name ? name.slice(0, 2).toUpperCase()
        : <IconUser size={Math.round(size * 0.52)} />}
    </div>
  );
}
