import { buildDiagramSvg } from "../../components/diagram/DiagramCanvas";
import {
  assertMermaidSourceWithinElementBudget,
  createDiagramDocumentFromMermaidSource,
  parseDiagramDocument,
} from "./schema";
import { createDiagramDocumentsFromDrawioSource } from "./drawioPreview";
import {
  DIAGRAM_PREVIEW_TOO_LARGE_MESSAGE,
  diagramPreviewSourceIsTooLarge,
} from "./previewLimits";

export { MAX_DIAGRAM_PREVIEW_BYTES } from "./previewLimits";

export type DiagramArtifactPreviewPage = { title: string; svg: string };
export type DiagramArtifactPreview = {
  svg: string;
  pages: DiagramArtifactPreviewPage[];
  error: string;
};

let mermaidInitialized = false;
let mermaidRenderId = 0;

type DiagramArtifactSourceKind = "native" | "mermaid" | "drawio";

function diagramArtifactSourceKind(fileType?: string): DiagramArtifactSourceKind | null {
  const normalized = String(fileType || "").trim().toLowerCase().replace(/^\./, "");
  if (normalized === "diagram.json") return "native";
  if (normalized === "mmd" || normalized === "mermaid") return "mermaid";
  if (normalized === "drawio") return "drawio";
  return null;
}

function isDrawioSource(content: string, title: string): boolean {
  return /\.drawio$/i.test(title)
    || /^\ufeff?\s*(?:<\?xml\b[\s\S]*?\?>\s*)?<(?:mxfile|mxGraphModel)\b/i.test(content);
}

function isNativeDiagramTitle(title: string): boolean {
  return /\.(?:diagram\.json|diagram)$/i.test(title.trim());
}

export function createDiagramArtifactPreview(content: string, title: string): DiagramArtifactPreview {
  const fallbackTitle = title.replace(/\.(diagram\.json|diagram|mmd|mermaid|drawio)$/i, "") || "Diagram";
  try {
    let document = createDiagramDocumentFromMermaidSource(content, fallbackTitle);
    if (!document) {
      document = parseDiagramDocument(content, fallbackTitle);
    }
    const svg = buildDiagramSvg(document, { background: false });
    return {
      svg,
      pages: [{ title: document.title, svg }],
      error: "",
    };
  } catch (error) {
    return {
      svg: "",
      pages: [],
      error: error instanceof Error ? error.message : "Diagram preview failed",
    };
  }
}

async function createMermaidArtifactPreview(content: string): Promise<DiagramArtifactPreview> {
  try {
    assertMermaidSourceWithinElementBudget(content);
    const { default: mermaid } = await import("mermaid");
    if (!mermaidInitialized) {
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        theme: "base",
        themeVariables: {
          fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif",
          primaryColor: "#f7f6f3",
          primaryTextColor: "#292524",
          primaryBorderColor: "#57534e",
          lineColor: "#57534e",
          secondaryColor: "#f2f1ee",
          tertiaryColor: "#ffffff",
        },
      });
      mermaidInitialized = true;
    }
    mermaidRenderId += 1;
    const rendered = await mermaid.render(`manor_mermaid_${mermaidRenderId}`, content);
    return { svg: rendered.svg, pages: [{ title: "Diagram", svg: rendered.svg }], error: "" };
  } catch (error) {
    return {
      svg: "",
      pages: [],
      error: error instanceof Error ? error.message : "Mermaid preview failed",
    };
  }
}

export async function createRenderedDiagramArtifactPreview(
  content: string,
  title: string,
  fileType?: string,
): Promise<DiagramArtifactPreview> {
  if (diagramPreviewSourceIsTooLarge(content)) {
    return { svg: "", pages: [], error: DIAGRAM_PREVIEW_TOO_LARGE_MESSAGE };
  }
  const sourceKind = diagramArtifactSourceKind(fileType);
  const genericDiagramKind = String(fileType || "").trim().toLowerCase().replace(/^\./, "") === "diagram";
  if (sourceKind === "drawio" || (!sourceKind && isDrawioSource(content, title))) {
    try {
      const fallbackTitle = title.replace(/\.drawio$/i, "") || "Diagram";
      const documents = await createDiagramDocumentsFromDrawioSource(content, fallbackTitle);
      const pages = documents.map((document) => ({
        title: document.title,
        svg: buildDiagramSvg(document, { background: false }),
      }));
      return { svg: pages[0]?.svg || "", pages, error: "" };
    } catch (error) {
      return {
        svg: "",
        pages: [],
        error: error instanceof Error ? error.message : "Draw.io preview failed",
      };
    }
  }

  if (sourceKind === "native") {
    return createDiagramArtifactPreview(content, title);
  }
  if (sourceKind === "mermaid") {
    return createMermaidArtifactPreview(content);
  }

  const trimmed = content.trimStart();
  if (!trimmed && isNativeDiagramTitle(title)) {
    return createDiagramArtifactPreview(content, title);
  }
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    return createDiagramArtifactPreview(content, title);
  }
  if (!trimmed && genericDiagramKind) {
    return createDiagramArtifactPreview(content, title);
  }
  return createMermaidArtifactPreview(content);
}
