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
  return {
    Icon: resolveWorkspaceIcon(iconName),
    label: ws.category || t("page.workspaces.workspace"),
    bg: "#f3f1ed",
    fg: "#6f6860",
    iconIndex,
    iconName,
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
        background: presentation.bg,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        boxShadow: "inset 0 0 0 1px rgba(28,25,23,0.035)",
        ...style,
      }}
    >
      <span aria-hidden="true" style={{ display: "inline-flex" }}>
        <presentation.Icon
          size={iconSize}
          style={{ color: presentation.fg }}
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
