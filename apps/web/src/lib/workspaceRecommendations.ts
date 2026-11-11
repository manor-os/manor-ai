// Ranks marketplace Blueprints against what the company already has, so creating
// a Workspace can start from a few relevant choices without a model call.

export interface RecommendableBlueprint {
  id: string;
  title: string;
  summary: string | null;
  tags: string[];
  install_count: number;
  required_integrations?: string[];
}

export interface RecommendationContext {
  workspaces: { name: string; description?: string | null; kind?: string | null }[];
  connectedIntegrations: string[];
  industry?: string | null;
}

export type RecommendationReason =
  | { kind: "integration"; name: string }
  | { kind: "workspace"; name: string }
  | { kind: "industry"; name: string }
  | { kind: "popular" };

export interface WorkspaceRecommendation<T extends RecommendableBlueprint> {
  blueprint: T;
  reason: RecommendationReason;
}

const IGNORED_WORDS = new Set([
  "and", "for", "the", "with", "from", "your", "workspace", "workspaces", "studio",
  "production", "agent", "agents", "team", "operator", "selected", "report", "reporting",
]);

const INDUSTRY_WORDS: Record<string, string[]> = {
  technology: ["product", "screen", "tutorial", "recording", "qa"],
  healthcare: ["handbook", "presentation", "tutorial"],
  finance: ["presentation", "report", "handbook"],
  education: ["tutorial", "handbook", "presentation", "podcast"],
  "e-commerce": ["product", "video", "carousel", "shorts"],
  "real estate": ["video", "carousel", "presentation"],
  legal: ["handbook", "presentation"],
  marketing: ["content", "video", "carousel", "shorts", "linkedin", "podcast"],
  manufacturing: ["product", "tutorial", "handbook"],
};

function words(text: string | null | undefined): Set<string> {
  const found = String(text || "").toLowerCase().match(/[a-z0-9]{3,}/g) || [];
  return new Set(found.filter((word) => !IGNORED_WORDS.has(word)));
}

function normalizedName(text: string): string {
  return text.trim().toLowerCase().replace(/\s+/g, " ");
}

export function recommendWorkspaces<T extends RecommendableBlueprint>(
  blueprints: T[],
  context: RecommendationContext,
  limit = 3,
): WorkspaceRecommendation<T>[] {
  const existingNames = new Set(context.workspaces.map((workspace) => normalizedName(workspace.name)));
  const industry = normalizedName(context.industry || "");
  const industryWords = INDUSTRY_WORDS[industry] || [];

  const candidates = blueprints
    .filter((blueprint) => !existingNames.has(normalizedName(blueprint.title)))
    .map((blueprint) => {
      const text = words(`${blueprint.title} ${blueprint.summary || ""} ${blueprint.tags.join(" ")}`);
      const required = new Set((blueprint.required_integrations || []).map((key) => key.toLowerCase()));

      const integration = context.connectedIntegrations.find((name) => {
        const key = name.toLowerCase();
        return required.has(key) || text.has(key);
      });
      let workspaceOverlap = 0;
      let relatedWorkspace = "";
      for (const workspace of context.workspaces) {
        const known = words(`${workspace.name} ${workspace.description || ""} ${workspace.kind || ""}`);
        const overlap = [...known].filter((word) => text.has(word)).length;
        if (overlap > workspaceOverlap) {
          workspaceOverlap = overlap;
          relatedWorkspace = workspace.name;
        }
      }
      const fitsIndustry = industryWords.some((word) => text.has(word));

      // One shared word ("video") is a coincidence, not a reason to say they relate.
      const related = workspaceOverlap >= 2;
      const score =
        (integration ? 4 : 0) +
        (related ? Math.min(workspaceOverlap, 3) : 0) +
        (fitsIndustry ? 2 : 0) +
        Math.log10(1 + Math.max(blueprint.install_count, 0));
      const reason: RecommendationReason = integration
        ? { kind: "integration", name: integration }
        : related
          ? { kind: "workspace", name: relatedWorkspace }
          : fitsIndustry
            ? { kind: "industry", name: context.industry || "" }
            : { kind: "popular" };
      return { blueprint, reason, score };
    });

  // Three of the same kind is one choice: each pick lowers its neighbours.
  const picked: WorkspaceRecommendation<T>[] = [];
  const remaining = [...candidates];
  while (picked.length < limit && remaining.length > 0) {
    const pickedTags = new Set(picked.flatMap((item) => item.blueprint.tags));
    let bestIndex = 0;
    let bestScore = -Infinity;
    remaining.forEach((candidate, index) => {
      const shared = candidate.blueprint.tags.filter((tag) => pickedTags.has(tag)).length;
      const adjusted = candidate.score - shared * 0.75;
      if (adjusted > bestScore) {
        bestScore = adjusted;
        bestIndex = index;
      }
    });
    const [best] = remaining.splice(bestIndex, 1);
    // The same reason three times stops being a reason.
    const repeated = best.reason.kind !== "popular" && picked.some((item) =>
      item.reason.kind === best.reason.kind && "name" in item.reason && "name" in best.reason && item.reason.name === best.reason.name);
    picked.push({ blueprint: best.blueprint, reason: repeated ? { kind: "popular" } : best.reason });
  }
  return picked;
}
