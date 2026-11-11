import type { CSSProperties, ReactNode } from "react";
import type { Workspace } from "../../lib/types";
import { t } from "../../lib/i18n";
import {
  IconTikTok,
  IconTwitter,
  IconYouTube,
  type IconProps,
} from "../icons";
import { WORKSPACE_LUCIDE_ICON_COMPONENTS } from "./WorkspaceLucideIcons";
import {
  matchWorkspacePresentationRule,
  workspaceMainIconIndex,
  workspaceMainIconName,
} from "./workspace-presentation.mjs";

export type WorkspaceIconComponent = (props: IconProps) => ReactNode;

export interface WorkspacePresentation {
  Icon: WorkspaceIconComponent;
  label: string;
  bg: string;
  fg: string;
  iconIndex: number;
  iconName: string;
}

export interface WorkspacePresentationSource {
  id?: string | null;
  name?: string | null;
  description?: string | null;
  category?: string | null;
  kind?: string | null;
  identity_label?: string | null;
  property_type?: string | null;
  primary_work?: string | null;
  operating_context?: string | null;
  attribute_tags?: string[] | null;
}

const BRAND_ICONS: Record<string, WorkspaceIconComponent> = {
  "brand:twitter": IconTwitter,
  "brand:tiktok": IconTikTok,
  "brand:youtube": IconYouTube,
};

const LUCIDE_ICON_COMPONENTS = WORKSPACE_LUCIDE_ICON_COMPONENTS as unknown as Record<
  string,
  WorkspaceIconComponent
>;

function resolveWorkspaceIcon(iconName: string): WorkspaceIconComponent {
  return (
    BRAND_ICONS[iconName]
    || LUCIDE_ICON_COMPONENTS[iconName]
    || LUCIDE_ICON_COMPONENTS.BriefcaseBusiness
  );
}

export function getWorkspacePresentation(ws: WorkspacePresentationSource): WorkspacePresentation {
  const rule = matchWorkspacePresentationRule(ws);
  const iconName = workspaceMainIconName(ws, rule?.iconKey);
  const iconIndex = workspaceMainIconIndex(ws, rule?.iconKey);
  if (rule) {
    return {
      Icon: resolveWorkspaceIcon(iconName),
      label: rule.label,
      bg: rule.bg,
      fg: rule.fg,
      iconIndex,
      iconName,
    };
  }
  // No category recognised: show the name's first character rather than an
  // unrelated glyph picked at random.
  const initial = Array.from((ws.name || "").trim())[0] || "";
  return {
    Icon: initial ? initialGlyph(initial) : resolveWorkspaceIcon(iconName),
    label: ws.category || t("page.workspaces.workspace"),
    bg: "#f3f1ed",
    fg: "#6f6860",
    iconIndex,
    iconName,
  };
}

function initialGlyph(initial: string): WorkspaceIconComponent {
  return function WorkspaceInitial({ size = 16, style }: IconProps) {
    return (
      <span style={{ fontSize: Number(size) * 0.92, fontWeight: 600, lineHeight: 1, ...style }}>
        {initial.toLocaleUpperCase()}
      </span>
    );
  };
}

export function WorkspacePresentationTile({
  presentation,
  size = 40,
  iconSize = Math.round(size * 0.5),
  style,
}: {
  presentation: WorkspacePresentation;
  size?: number;
  iconSize?: number;
  style?: CSSProperties;
}) {
  return (
    <div
      data-workspace-main-icon={presentation.iconName}
      data-workspace-main-icon-index={presentation.iconIndex}
      style={{
        width: size,
        height: size,
        minWidth: size,
        borderRadius: Math.round(size * 0.25),
        // One glazed, monochrome tile for every Workspace: the glyph tells them apart.
        background: "linear-gradient(180deg, var(--surface-panel) 0%, var(--surface-muted) 100%)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        boxShadow: "inset 0 0 0 0.5px rgba(0,0,0,0.08), inset 0 1px 0 rgba(255,255,255,0.9), 0 1px 2px rgba(0,0,0,0.05), 0 4px 10px -4px rgba(0,0,0,0.12)",
        ...style,
      }}
    >
      <span aria-hidden="true" style={{ display: "inline-flex" }}>
        <presentation.Icon
          size={iconSize}
          style={{ color: "var(--text-strong)" }}
        />
      </span>
    </div>
  );
}

export default function WorkspaceIconTile({
  workspace,
  size = 40,
  iconSize = Math.round(size * 0.5),
  style,
}: {
  workspace: Workspace;
  size?: number;
  iconSize?: number;
  style?: CSSProperties;
}) {
  return (
    <WorkspacePresentationTile
      presentation={getWorkspacePresentation(workspace)}
      size={size}
      iconSize={iconSize}
      style={style}
    />
  );
}
