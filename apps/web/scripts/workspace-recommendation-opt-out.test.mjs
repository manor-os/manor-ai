import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const chatSource = fs.readFileSync(
  new URL("../src/components/EmbeddedChat.tsx", import.meta.url),
  "utf8",
);
const floatingChatSource = fs.readFileSync(
  new URL("../src/components/FloatingChat.tsx", import.meta.url),
  "utf8",
);
const cardSource = fs.readFileSync(
  new URL("../src/components/WorkspaceRecommendationCard.tsx", import.meta.url),
  "utf8",
);
const recommendationActionsSource = fs.readFileSync(
  new URL("../src/components/useWorkspaceRecommendationActions.ts", import.meta.url),
  "utf8",
);

test("Workspace recommendations expose separate one-time and persistent dismissals", () => {
  assert.match(cardSource, /onContinue: \(\) => void/);
  assert.match(cardSource, /onDontSuggestAgain: \(\) => void/);
  assert.match(
    cardSource,
    /component\.workspace_recommendation\.dont_suggest_again/,
  );
  assert.match(
    recommendationActionsSource,
    /api\.admin\.updatePreferences\(\{[\s\S]*?workspace_recommendations_enabled:\s*false/,
  );
  assert.match(recommendationActionsSource, /queryKey:\s*\["preferences"\]/);
  assert.match(recommendationActionsSource, /queryClient\.setQueryData\(\["preferences"\]/);
  assert.match(recommendationActionsSource, /setWorkspaceRecommendationsSuppressed\(true\)/);
  assert.match(
    chatSource,
    /workspaceRecommendation\s*&&[\s\S]*?workspaceRecommendationPreferencesReady[\s\S]*?!workspaceRecommendationsSuppressed/,
  );
});

test("Workspace recommendations render in the floating chat surface", () => {
  assert.match(floatingChatSource, /normalizeWorkspaceRecommendation/);
  assert.match(floatingChatSource, /<WorkspaceRecommendationCard/);
  assert.match(
    floatingChatSource,
    /workspaceRecommendation\s*&&[\s\S]*?workspaceRecommendationPreferencesReady[\s\S]*?!workspaceRecommendationsSuppressed/,
  );
  assert.match(
    floatingChatSource,
    /useWorkspaceRecommendationActions/,
  );
});
