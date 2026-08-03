import type { CSSProperties, ReactNode } from "react";

import GlassCard from "./GlassCard";

export const WORKSPACE_APP_CARD_HEIGHT = 252;
export const WORKSPACE_APP_CARD_MIN_WIDTH = 300;
export const WORKSPACE_APP_CARD_GAP = 14;

export const workspaceAppCardGridStyle: CSSProperties = {
  display: "grid",
  gridTemplateColumns: `repeat(auto-fill, minmax(min(100%, ${WORKSPACE_APP_CARD_MIN_WIDTH}px), 1fr))`,
  gap: WORKSPACE_APP_CARD_GAP,
};

interface WorkspaceAppCardProps {
  children: ReactNode;
  className?: string;
  footer?: ReactNode;
  flush?: boolean;
  onClick?: () => void;
  onContextMenu?: React.MouseEventHandler<HTMLDivElement>;
  style?: CSSProperties;
}

export default function WorkspaceAppCard({
  children,
  className = "",
  footer,
  flush = false,
  onClick,
  onContextMenu,
  style,
}: WorkspaceAppCardProps) {
  return (
    <GlassCard
      className={`workspace-app-card ${className}`.trim()}
      onClick={onClick}
      onContextMenu={onContextMenu}
      footer={footer}
      style={{
        height: WORKSPACE_APP_CARD_HEIGHT,
        display: "flex",
        flexDirection: "column",
        justifyContent: "space-between",
        overflow: "hidden",
        ...(flush ? { padding: 0 } : {}),
        ...style,
      }}
    >
      {children}
    </GlassCard>
  );
}
