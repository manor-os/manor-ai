export type SoloBusinessIdeaDefinition = {
  id: string;
  tags: readonly [string, string];
  manorExecution: ManorIdeaExecution;
};

export type ManorIdeaExecution = "native" | "orchestrated" | "external";

export const SOLO_BUSINESS_IDEA_LIBRARY: readonly SoloBusinessIdeaDefinition[] = [
  {
    id: "marketplace-photo-studio",
    tags: ["micro-saas", "ecommerce"],
    manorExecution: "orchestrated",
  },
  {
    id: "spreadsheet-margin-copilot",
    tags: ["micro-saas", "b2b"],
    manorExecution: "native",
  },
  {
    id: "workspace-approval-addon",
    tags: ["marketplace-addon", "b2b"],
    manorExecution: "orchestrated",
  },
  {
    id: "open-core-log-scrubber",
    tags: ["open-source", "developer-tool"],
    manorExecution: "orchestrated",
  },
  {
    id: "trade-exam-coach",
    tags: ["mobile-app", "digital-product"],
    manorExecution: "external",
  },
  {
    id: "specialist-software-directory",
    tags: ["data-product", "paid-media"],
    manorExecution: "native",
  },
  {
    id: "customer-proof-collector",
    tags: ["micro-saas", "creator"],
    manorExecution: "native",
  },
  {
    id: "technical-manual-assistant",
    tags: ["micro-saas", "b2b"],
    manorExecution: "native",
  },
  {
    id: "tender-intelligence-brief",
    tags: ["data-product", "subscription"],
    manorExecution: "native",
  },
  {
    id: "role-specific-template-system",
    tags: ["template", "digital-product"],
    manorExecution: "native",
  },
  {
    id: "launch-design-subscription",
    tags: ["productized-service", "creator"],
    manorExecution: "native",
  },
  {
    id: "web-change-api",
    tags: ["api", "developer-tool"],
    manorExecution: "orchestrated",
  },
  {
    id: "seller-research-extension",
    tags: ["browser-extension", "ecommerce"],
    manorExecution: "orchestrated",
  },
  {
    id: "expert-transition-course",
    tags: ["education", "creator"],
    manorExecution: "external",
  },
  {
    id: "freelancer-utility-portfolio",
    tags: ["micro-saas", "digital-product"],
    manorExecution: "orchestrated",
  },
  {
    id: "maker-preorder-addon",
    tags: ["marketplace-addon", "ecommerce"],
    manorExecution: "orchestrated",
  },
  {
    id: "agency-benchmark-club",
    tags: ["data-product", "community"],
    manorExecution: "native",
  },
  {
    id: "creator-asset-shop",
    tags: ["digital-product", "creator"],
    manorExecution: "external",
  },
] as const;

export const SOLO_BUSINESS_IDEA_COUNT = SOLO_BUSINESS_IDEA_LIBRARY.length;

export type SoloBusinessIdeaField =
  | "title"
  | "buyer"
  | "promise"
  | "revenue"
  | "signal"
  | "test"
  | "manorPath";

export function soloBusinessIdeaKey(
  idea: SoloBusinessIdeaDefinition,
  field: SoloBusinessIdeaField,
) {
  return `component.embedded_chat.idea_library.${idea.id}.${field}`;
}

export function soloBusinessIdeaExecutionKey(execution: ManorIdeaExecution) {
  return `component.embedded_chat.idea_library.execution.${execution}`;
}

export function pickRandomSoloBusinessIdeas(
  count = 3,
  excludedIds: readonly string[] = [],
): SoloBusinessIdeaDefinition[] {
  const excluded = new Set(excludedIds);
  const freshPool = SOLO_BUSINESS_IDEA_LIBRARY.filter(
    (idea) => !excluded.has(idea.id),
  );
  const pool = freshPool.length >= count
    ? [...freshPool]
    : [...SOLO_BUSINESS_IDEA_LIBRARY];

  for (let index = pool.length - 1; index > 0; index -= 1) {
    const swapIndex = Math.floor(Math.random() * (index + 1));
    [pool[index], pool[swapIndex]] = [pool[swapIndex], pool[index]];
  }

  return pool.slice(0, Math.min(count, pool.length));
}
