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

export type WorkspaceIconComponent = (props: IconProps) => ReactNode;

export interface WorkspacePresentation {
  Icon: WorkspaceIconComponent;
  label: string;
  bg: string;
  fg: string;
}

interface WorkspacePresentationRule {
  terms: string[];
  Icon: WorkspaceIconComponent;
  label: string;
  bg: string;
  fg: string;
}

const WORKSPACE_PRESENTATION_RULES: WorkspacePresentationRule[] = [
  {
    terms: ["leasing", "lease", "property", "real estate", "rent", "occupancy", "tenant"],
    Icon: IconBuilding,
    label: "Property ops",
    bg: "#f2eee8",
    fg: "#75695e",
  },
  {
    terms: ["qa", "smoke", "test", "runtime", "regression"],
    Icon: IconBeaker,
    label: "QA runtime",
    bg: "#edf1ee",
    fg: "#65786e",
  },
  {
    terms: ["x account", "twitter", "threads", "social channel", "social account"],
    Icon: IconTwitter,
    label: "Social channel",
    bg: "#eef1f4",
    fg: "#647382",
  },
  {
    terms: ["tiktok"],
    Icon: IconTikTok,
    label: "Short video",
    bg: "#f1ecef",
    fg: "#7a6570",
  },
  {
    terms: ["video", "youtube", "creator", "content"],
    Icon: IconYouTube,
    label: "Content studio",
    bg: "#f3eeee",
    fg: "#7b665f",
  },
  {
    terms: ["store", "shopify", "commerce", "ecommerce", "product", "order"],
    Icon: IconStore,
    label: "Store ops",
    bg: "#f3efe7",
    fg: "#766b58",
  },
  {
    terms: ["support", "customer", "inbox", "ticket", "community"],
    Icon: IconChat,
    label: "Support desk",
    bg: "#eef1ec",
    fg: "#667569",
  },
  {
    terms: ["ai", "tech", "founder", "launch", "startup"],
    Icon: IconRocket,
    label: "Founder OS",
    bg: "#f2efe9",
    fg: "#706a60",
  },
  {
    terms: ["sales", "outreach", "pipeline", "revenue", "crm"],
    Icon: IconMegaphone,
    label: "Revenue room",
    bg: "#f3efe9",
    fg: "#76685c",
  },
  {
    terms: ["engineering", "code", "developer", "software"],
    Icon: IconCode,
    label: "Engineering",
    bg: "#eef0f1",
    fg: "#68727a",
  },
  {
    terms: ["research", "learning", "course", "training"],
    Icon: IconAcademicCap,
    label: "Research",
    bg: "#f0eee7",
    fg: "#716b5e",
  },
  {
    terms: ["brand", "website", "marketing", "campaign"],
    Icon: IconGlobe,
    label: "Growth",
    bg: "#f0f0e9",
    fg: "#6d705f",
  },
  {
    terms: ["compliance", "security", "policy", "approval"],
    Icon: IconShield,
    label: "Governance",
    bg: "#efefec",
    fg: "#6f6d68",
  },
  {
    terms: ["project", "launch", "operation", "ops"],
    Icon: IconChecklist,
    label: "Operations",
    bg: "#f2efe9",
    fg: "#73695f",
  },
];

function workspaceHaystack(ws: Workspace): string {
  return [
    ws.name,
    ws.description,
    ws.category,
    ws.kind,
    ws.identity_label,
    ws.property_type,
    ws.primary_work,
    ws.operating_context,
    ...(ws.attribute_tags || []),
  ].filter(Boolean).join(" ").toLowerCase();
}

export function getWorkspacePresentation(ws: Workspace): WorkspacePresentation {
  const haystack = workspaceHaystack(ws);
  const match = WORKSPACE_PRESENTATION_RULES.find((rule) =>
    rule.terms.some((term) => haystack.includes(term)),
  );
  if (match) {
    return match;
  }
  return {
    Icon: IconBriefcase,
    label: ws.category || t("page.workspaces.workspace"),
    bg: "#f3f1ed",
    fg: "#6f6860",
  };
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
  const presentation = getWorkspacePresentation(workspace);
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
