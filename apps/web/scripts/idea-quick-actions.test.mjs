import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";


const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (relativePath) =>
  readFile(path.join(webRoot, relativePath), "utf8");

const [
  chatSource,
  messageDisplaySource,
  cssSource,
  apiSource,
  enSource,
  zhSource,
  esSource,
  librarySource,
  skillSource,
  skillLibrarySource,
  casePatternsSource,
  manorExecutionSource,
  reviewSkillSource,
  reviewExecutionSource,
] =
  await Promise.all([
    read("src/components/EmbeddedChat.tsx"),
    read("src/components/ChatMessageDisplay.tsx"),
    read("src/index.css"),
    read("src/lib/api.ts"),
    read("src/lib/i18n/en.ts"),
    read("src/lib/i18n/zh.ts"),
    read("src/lib/i18n/es.ts"),
    read("src/lib/soloBusinessIdeas.ts"),
    read("../../packages/core/ai/skills/solo-business-idea-finder/SKILL.md"),
    read("../../packages/core/ai/skills/solo-business-idea-finder/references/starter-idea-library.md"),
    read("../../packages/core/ai/skills/solo-business-idea-finder/references/opc-case-patterns.md"),
    read("../../packages/core/ai/skills/solo-business-idea-finder/references/manor-execution-map.md"),
    read("../../packages/core/ai/skills/solo-business-idea-review/SKILL.md"),
    read("../../packages/core/ai/skills/solo-business-idea-review/references/manor-execution-check.md"),
  ]);

test("chat home centers two idea actions inside the capability rail", () => {
  assert.ok(chatSource.includes('id: "new-idea"'));
  assert.ok(chatSource.includes("icon: IconSparkles"));
  assert.ok(chatSource.includes('id: "validate-idea"'));
  assert.ok(chatSource.includes("icon: IconReport"));
  assert.ok(chatSource.includes('useState<WorkspaceRailKey>("new-idea")'));
  assert.ok(chatSource.includes('"new-idea",\n  "validate-idea",\n  "workspace"'));
  assert.ok(chatSource.includes("const pairCenter ="));
  assert.ok(chatSource.includes("pairCenter - rail.clientWidth / 2"));
  assert.ok(chatSource.includes("const resizeObserver = new ResizeObserver"));
  assert.ok(chatSource.includes('centerFocusedItems("auto")'));
  assert.ok(chatSource.includes('data-pair-focused={railFocusKey === "new-idea"'));
  assert.ok(chatSource.includes("handleRailItemClick(railKey)"));
  assert.ok(chatSource.includes("const dockClickRailKeyRef"));
  assert.ok(chatSource.includes("if (clickedKey) handleRailItemClick(clickedKey)"));
  assert.ok(chatSource.includes("if (event.detail === 0) handleRailItemClick(railKey)"));
  assert.ok(chatSource.includes('event.key !== "Enter" && event.key !== " "'));
  assert.ok(chatSource.includes("event.preventDefault();\n                handleRailItemClick(railKey);"));
  assert.ok(chatSource.includes("pickRandomSoloBusinessIdeas"));
  assert.ok(chatSource.includes("ideaCandidateRequest(focusedIdea, idea)"));
  assert.ok(chatSource.includes("workspace-sample-card workspace-idea-card"));
  assert.ok(chatSource.includes('focusedIdea?.id === "validate-idea"'));
  assert.ok(chatSource.includes("workspace-idea-validation-intake"));
  assert.ok(chatSource.includes("onValidationStart"));
  assert.ok(chatSource.includes('setIdeaComposerMode("validate-idea")'));
  assert.ok(chatSource.includes("composerEditorRef.current?.focus()"));
  assert.ok(chatSource.includes("editorRef={composerEditorRef}"));
  assert.ok(chatSource.includes("automaticIdeaSkill"));
  assert.ok(chatSource.includes('role="button"\n                  tabIndex={0}'));
  assert.ok(chatSource.includes("refreshIdeaCandidates"));
  assert.ok(chatSource.includes("freshIdeaRequest()"));
  assert.ok(chatSource.includes("workspace-idea-summary"));
  assert.ok(chatSource.includes('ideaField(idea, "revenue")'));
});

test("idea modes draw clickable candidates from an extensible library", () => {
  assert.equal(
    (librarySource.match(/\bid: "/g) || []).length,
    18,
    "the researched library should contain eighteen distinct idea definitions",
  );
  assert.equal(
    (librarySource.match(/manorExecution: "native"/g) || []).length,
    8,
    "eight starters should have a Manor-native core delivery path",
  );
  assert.equal(
    (librarySource.match(/manorExecution: "orchestrated"/g) || []).length,
    7,
    "seven starters should require an external customer runtime",
  );
  assert.equal(
    (librarySource.match(/manorExecution: "external"/g) || []).length,
    3,
    "three starters should depend on an external core capability",
  );
  assert.ok(librarySource.includes('id: "open-core-log-scrubber"'));
  assert.ok(librarySource.includes('id: "seller-research-extension"'));
  assert.ok(librarySource.includes('id: "creator-asset-shop"'));
  assert.equal(librarySource.includes('id: "security-questionnaire-sprint"'), false);
  assert.ok(librarySource.includes("excludedIds"));
  assert.ok(librarySource.includes("Math.random()"));
  assert.ok(chatSource.includes('aria-live="polite"'));
  assert.ok(chatSource.includes("onIdeaQuickAction(ideaCandidateRequest"));
  assert.ok(cssSource.includes(".workspace-idea-card:focus-visible"));
  assert.ok(cssSource.includes(".workspace-idea-refresh:hover"));
  assert.ok(cssSource.includes(".workspace-idea-summary"));
  assert.ok(cssSource.includes(".workspace-idea-actions"));
  assert.ok(cssSource.includes(".workspace-idea-validation-intake"));
  assert.ok(
    cssSource.includes(
      ".workspace-idea-validation-checklist {\n    grid-template-columns: minmax(0, 1fr);",
    ),
  );
  const uiIdeaIds = [...librarySource.matchAll(/\bid: "([^"]+)"/g)]
    .map((match) => match[1])
    .sort();
  const skillIdeaIds = [...skillLibrarySource.matchAll(/^## ([a-z0-9-]+)$/gm)]
    .map((match) => match[1])
    .sort();
  assert.deepEqual(uiIdeaIds, skillIdeaIds);
  const ideaTags = [
    ...librarySource.matchAll(/tags: \["([^"]+)", "([^"]+)"\]/g),
  ].flatMap((match) => [match[1], match[2]]);
  for (const source of [enSource, zhSource]) {
    for (const id of uiIdeaIds) {
      for (const field of ["title", "buyer", "promise", "revenue", "signal", "test", "manorPath"]) {
        assert.ok(
          source.includes(`"component.embedded_chat.idea_library.${id}.${field}"`),
          `missing localized ${field} for ${id}`,
        );
      }
    }
    for (const tag of new Set(ideaTags)) {
      assert.ok(
        source.includes(`"component.embedded_chat.idea_library.tag.${tag}"`),
        `missing localized tag ${tag}`,
      );
    }
  }
  assert.ok(skillSource.includes("references/opc-case-patterns.md"));
  assert.ok(skillSource.includes("references/manor-execution-map.md"));
  assert.ok(skillSource.includes("Candidate Deep Dive"));
  assert.ok(skillLibrarySource.includes("not the total idea supply"));
  assert.equal(
    (skillLibrarySource.match(/\*\*How it earns:\*\*/g) || []).length,
    18,
  );
  assert.ok(manorExecutionSource.includes("Eight can currently deliver"));
  assert.ok(manorExecutionSource.includes("Import quote profit checker"));
  assert.ok(reviewSkillSource.includes("references/manor-execution-check.md"));
  assert.ok(reviewExecutionSource.includes("Map the workflow"));
  assert.ok(casePatternsSource.includes("It does not contain a\nprewritten business-idea database."));
  assert.ok(casePatternsSource.includes("Photopea"));
  assert.ok(casePatternsSource.includes("Sidekiq"));
  assert.ok(casePatternsSource.includes("Superpower ChatGPT"));
  assert.ok(casePatternsSource.includes("CC-BY-NC-SA-4.0"));
});

test("idea rail actions are accessible, responsive, and motion-safe", () => {
  assert.ok(
    chatSource.includes(
      'aria-label={t("component.embedded_chat.workspace_capability_selector")}',
    ),
  );
  assert.ok(chatSource.includes('role="group"'));
  assert.ok(chatSource.includes("aria-current={isActive ? \"true\" : undefined}"));
  assert.ok(cssSource.includes(".workspace-mode-pill:focus-visible"));
  assert.ok(cssSource.includes('.workspace-mode-rail[data-pair-focused="true"]'));
  assert.ok(cssSource.includes(".workspace-mode-pill--idea"));
  assert.ok(cssSource.includes(".workspace-mode-pill--idea.workspace-mode-pill--active"));
  assert.ok(
    cssSource.includes(
      ".workspace-mode-pill--idea.workspace-mode-pill--active {\n  width: 132px;",
    ),
  );
  assert.ok(
    cssSource.includes(
      ".embedded-chat-body--empty .workspace-mode-pill--idea {\n  width: 132px;\n  height: 68px;",
    ),
  );
  assert.ok(
    cssSource.includes(
      ".embedded-chat-body--empty .workspace-mode-summary {\n  max-width: min(100%, 600px);\n  margin-top: 12px;",
    ),
  );
  assert.ok(cssSource.includes(".workspace-mode-pill {\n    animation: none;"));
  assert.equal(cssSource.includes(".workspace-idea-quick-actions"), false);
});

test("idea quick actions bind built-in skills without exposing implementation prompts", () => {
  for (const source of [enSource, zhSource, esSource]) {
    assert.ok(source.includes('"component.embedded_chat.new_idea_today"'));
    assert.ok(source.includes('"component.embedded_chat.validate_my_idea"'));
    assert.equal(source.includes("Use the built-in solo-business-idea"), false);
    assert.equal(source.includes("使用默认内置的 solo-business-idea"), false);
    assert.equal(source.includes("Usa la Skill integrada solo-business-idea"), false);
  }
  assert.ok(chatSource.includes('id: "solo-business-idea-finder"'));
  assert.ok(chatSource.includes('id: "solo-business-idea-review"'));
  assert.ok(chatSource.includes("skill: IDEA_BUILT_IN_SKILLS[action.id]"));
  assert.ok(
    chatSource.includes("void handleSend(request.message, [], [request.skill]"),
  );
  assert.ok(
    apiSource.includes('form.append("manual_skill_ids", opts.manualSkillIds.join(","))'),
  );
  assert.ok(messageDisplaySource.includes("PRODUCT_CAPABILITY_SKILL_IDS"));
  assert.ok(messageDisplaySource.includes('"solo-business-idea-finder"'));
  assert.ok(messageDisplaySource.includes('"solo-business-idea-review"'));
  assert.ok(
    messageDisplaySource.includes("clean && !isProductCapabilitySkill(clean)"),
  );
  assert.equal(chatSource.includes("idea_library.explore_skill_prompt"), false);
  assert.equal(chatSource.includes("idea_library.validate_skill_prompt"), false);
  assert.ok(enSource.includes('"Ideas for you"'));
  assert.ok(zhSource.includes('"给你的新创意"'));
  assert.ok(esSource.includes('"Ideas para ti"'));
  assert.equal(enSource.includes('"OPC business ideas"'), false);
  assert.equal(zhSource.includes('"OPC 一人公司创意"'), false);
  assert.ok(zhSource.includes("先看 3 个种子创意"));
  assert.ok(zhSource.includes("根据我的情况生成全新创意"));
  assert.ok(zhSource.includes("用四个事实说明你的创意"));
  assert.ok(chatSource.includes('variant="primary"'));
  assert.ok(chatSource.includes("onValidationStart();"));
  assert.equal(chatSource.includes("workspace-idea-validation-cta"), false);
  assert.ok(cssSource.includes(".workspace-idea-summary dd"));
  assert.ok(cssSource.includes("font-size: 11px"));
  assert.ok(skillLibrarySource.includes("None of the 18 starters is itself"));
});
