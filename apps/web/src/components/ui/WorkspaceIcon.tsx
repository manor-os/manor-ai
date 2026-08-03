import type { CSSProperties, ReactNode } from "react";
import type { Workspace } from "../../lib/types";
import { t } from "../../lib/i18n";
import {
  IconAcademicCap,
  IconBeaker,
  IconBriefcase,
  IconBuilding,
  IconChat,
  IconChecklist,
  IconCode,
  IconGlobe,
  IconMegaphone,
  IconRocket,
  IconShield,
  IconStore,
  IconTikTok,
  IconTwitter,
  IconYouTube,
  type IconProps,
} from "../icons";
import { matchWorkspacePresentationRule } from "./workspace-presentation.mjs";

export type WorkspaceIconComponent = (props: IconProps) => ReactNode;

export interface WorkspacePresentation {
  Icon: WorkspaceIconComponent;
  label: string;
  bg: string;
  fg: string;
}

export interface WorkspacePresentationSource {
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

// The matching rule set and keyword logic live in workspace-presentation.mjs
// as plain, testable JS (word-boundary matching — see that file for why
// substring matching was wrong). This map is just the icon each rule's
// `iconKey` renders as, which is a React concern the .mjs module can't hold.
const PRESENTATION_ICONS: Record<string, WorkspaceIconComponent> = {
  building: IconBuilding,
  beaker: IconBeaker,
  twitter: IconTwitter,
  tiktok: IconTikTok,
  youtube: IconYouTube,
  store: IconStore,
  chat: IconChat,
  rocket: IconRocket,
  megaphone: IconMegaphone,
  code: IconCode,
  academicCap: IconAcademicCap,
  globe: IconGlobe,
  shield: IconShield,
  checklist: IconChecklist,
};

export function getWorkspacePresentation(ws: WorkspacePresentationSource): WorkspacePresentation {
  const rule = matchWorkspacePresentationRule(ws);
  if (rule) {
    return {
      Icon: PRESENTATION_ICONS[rule.iconKey] || IconBriefcase,
      label: rule.label,
      bg: rule.bg,
      fg: rule.fg,
    };
  }
  return {
    Icon: IconBriefcase,
    label: ws.category || t("page.workspaces.workspace"),
    bg: "#f3f1ed",
    fg: "#6f6860",
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
      <presentation.Icon size={iconSize} style={{ color: presentation.fg }} />
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
