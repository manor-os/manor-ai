import "./WorkspaceRailVisual.css";

export type WorkspaceRailVisualKind =
  | "research"
  | "agents"
  | "automations"
  | "flows"
  | "new-idea"
  | "validate-idea"
  | "workspace"
  | "slides"
  | "docs"
  | "pdf"
  | "sheets"
  | "website"
  | "image"
  | "video";

interface WorkspaceRailVisualProps {
  kind: WorkspaceRailVisualKind;
  active?: boolean;
}

const RAIL_VISUAL_SRC: Record<WorkspaceRailVisualKind, string> = {
  research: "/assets/workspace-rail-v2/research.png",
  agents: "/assets/workspace-rail-v2/agents.png",
  automations: "/assets/workspace-rail-v2/automations.png",
  flows: "/assets/workspace-rail-v2/flows.png",
  "new-idea": "/assets/workspace-rail-v2/new-idea.png",
  "validate-idea": "/assets/workspace-rail-v2/validate-idea.png",
  workspace: "/assets/workspace-rail-v2/workspace.png",
  slides: "/assets/workspace-rail-v2/slides.png",
  docs: "/assets/workspace-rail-v2/docs.png",
  pdf: "/assets/workspace-rail-v2/pdf.png",
  sheets: "/assets/workspace-rail-v2/sheets.png",
  website: "/assets/workspace-rail-v2/website.png",
  image: "/assets/workspace-rail-v2/image.png",
  video: "/assets/workspace-rail-v2/video.png",
};

export default function WorkspaceRailVisual({
  kind,
  active = false,
}: WorkspaceRailVisualProps) {
  return (
    <span
      aria-hidden="true"
      className="workspace-rail-visual-shell"
      data-active={active ? "true" : undefined}
      data-kind={kind}
    >
      <img
        className="workspace-rail-visual"
        draggable={false}
        src={RAIL_VISUAL_SRC[kind]}
      />
    </span>
  );
}
