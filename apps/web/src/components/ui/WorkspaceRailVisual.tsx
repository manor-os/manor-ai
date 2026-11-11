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

const DOCUMENT_PAGE = "M4 2.5h5.5l3 3v8H4zM9.5 2.5v3h3";

const RAIL_VISUAL_PATHS: Record<WorkspaceRailVisualKind, string> = {
  research: "M7 11.5a4.5 4.5 0 1 0 0-9 4.5 4.5 0 0 0 0 9zM10.5 10.5l3 3",
  agents: "M6 7a2.3 2.3 0 1 0 0-4.6A2.3 2.3 0 0 0 6 7zM2 13c.4-2.4 2-3.8 4-3.8s3.6 1.4 4 3.8M11 7.2a1.9 1.9 0 1 0-.6-3.7M11.4 9.4c1.4.4 2.3 1.7 2.6 3.6",
  automations: "M13 8a5 5 0 0 1-8.6 3.5M3 8a5 5 0 0 1 8.6-3.5M11.8 2.2v2.5H9.3M4.2 13.8v-2.5h2.5",
  flows: "M4 6a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3zM12 9.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3zM4 13a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3zM5.5 4.5H8a2 2 0 0 1 2 2v.5M5.5 11.5H8a2 2 0 0 0 2-2V9",
  "new-idea": "M8 2.5l1.6 3.9 3.9 1.6-3.9 1.6L8 13.5l-1.6-3.9L2.5 8l3.9-1.6z",
  "validate-idea": "M8 13.5a5.5 5.5 0 1 0 0-11 5.5 5.5 0 0 0 0 11zM5.5 8.2l1.8 1.8 3.2-3.6",
  workspace: "M2.5 3.5h11v9h-11zM6.5 3.5v9M6.5 7h7",
  slides: "M2.5 3.5h11v7.5h-11zM8 11v2.5M5.5 13.5h5M5.5 8.5V7M8 8.5v-3M10.5 8.5V6.5",
  docs: `${DOCUMENT_PAGE}M6 8.5h4.5M6 11h4.5`,
  pdf: `${DOCUMENT_PAGE}M6 11.5v-3h1.3a1 1 0 0 1 0 2H6`,
  sheets: "M2.5 3.5h11v9h-11zM2.5 6.5h11M2.5 9.5h11M6.5 3.5v9",
  website: "M8 13.5A5.5 5.5 0 1 0 8 2.5a5.5 5.5 0 0 0 0 11zM2.5 8h11M8 2.5c1.6 1.6 2.3 3.4 2.3 5.5S9.6 11.9 8 13.5C6.4 11.9 5.7 10.1 5.7 8S6.4 4.1 8 2.5z",
  image: "M2.5 3.5h11v9h-11zM2.5 11l3-3 2.5 2.5L10.5 8l3 3M5.5 6.5h.01",
  video: "M2.5 5h7.5v6H2.5zM10 7l3.5-2v6L10 9",
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
      <svg
        className="workspace-rail-visual"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d={RAIL_VISUAL_PATHS[kind]} />
      </svg>
    </span>
  );
}
