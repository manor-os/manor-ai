import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";


const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (relativePath) =>
  readFile(path.join(webRoot, relativePath), "utf8");

const [
  chatSource,
  railVisualSource,
  railVisualCssSource,
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
    read("src/components/ui/WorkspaceRailVisual.tsx"),
    read("src/components/ui/WorkspaceRailVisual.css"),
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
  assert.ok(chatSource.includes('id: "validate-idea"'));
  assert.ok(chatSource.includes("activeIdeaMode || activeCapability"));
  assert.ok(chatSource.includes("<WorkspaceRailVisual kind={railKey} active={isActive} />"));
  assert.ok(chatSource.includes('"new-idea",\n  "validate-idea",\n  "workspace"'));
  assert.ok(chatSource.includes("const pairCenter ="));
  assert.ok(chatSource.includes("pairCenter - rail.clientWidth / 2"));
  assert.ok(chatSource.includes("const resizeObserver = new ResizeObserver"));
  assert.ok(chatSource.includes('centerFocusedItems("auto")'));
  assert.ok(chatSource.includes('data-pair-focused={railFocusKey === "new-idea"'));
  assert.ok(chatSource.includes("const focusRailByStep ="));
  // Direction only — no per-key clamp, or the keys behind it become
  // click-only. See workspace-mode-rail.test.mjs.
  assert.ok(chatSource.includes("focusRailByStep(delta > 0 ? 1 : -1)"));
  assert.ok(chatSource.includes("wheelSwitchLockedRef.current) return"));
  assert.ok(chatSource.includes("}, 420)"));
  assert.ok(chatSource.includes("focusRailByStep(diff < 0 ? 1 : -1)"));
  assert.ok(chatSource.includes("handleRailItemClick(railKey)"));
  assert.ok(chatSource.includes("const dockClickRailKeyRef"));
  assert.ok(chatSource.includes("if (clickedKey) handleRailItemClick(clickedKey)"));
  assert.ok(chatSource.includes("const chatModeRef = useRef(chatMode)"));
  // A new chat still pre-focuses the "new idea" card, but only cosmetically:
  // the re-arm is tagged origin "default" so it cannot attach a skill.
  assert.ok(
    chatSource.includes(
      'chatModeRef.current === "auto"\n          ? { mode: "new-idea", origin: "default" }\n          : null,',
    ),
  );
  assert.ok(chatSource.includes("if (event.detail === 0) handleRailItemClick(railKey)"));
  assert.ok(chatSource.includes('event.key !== "Enter" && event.key !== " "'));
  assert.ok(chatSource.includes("event.preventDefault();\n                handleRailItemClick(railKey);"));
  assert.ok(chatSource.includes("pickRandomSoloBusinessIdeas"));
  assert.ok(chatSource.includes("ideaCandidateRequest(focusedIdea, idea)"));
  assert.ok(chatSource.includes("workspace-sample-card workspace-idea-card"));
  assert.ok(chatSource.includes('focusedIdea?.id === "validate-idea"'));
  assert.ok(chatSource.includes("workspace-idea-validation-intake"));
  assert.ok(chatSource.includes("onValidationStart"));
  assert.ok(
    chatSource.includes(
      'setIdeaComposer({ mode: "validate-idea", origin: "user" })',
    ),
  );
  assert.ok(chatSource.includes("composerEditorRef.current?.focus()"));
  assert.ok(chatSource.includes("editorRef={composerEditorRef}"));
  // Rail clicks and wheel focus are deliberate gestures, so they may arm a skill.
  assert.ok(
    chatSource.includes('setIdeaComposer(mode ? { mode, origin: "user" } : null)'),
  );
  assert.ok(chatSource.includes('role="button"\n                  tabIndex={0}'));
  assert.ok(chatSource.includes("refreshIdeaCandidates"));
  assert.ok(chatSource.includes("freshIdeaRequest()"));
  assert.ok(chatSource.includes("workspace-idea-summary"));
  assert.ok(chatSource.includes('ideaField(idea, "revenue")'));
});

/*
 * manual_skill_ids is not a hint: the server treats it as an explicit user
 * selection and force-invokes the skill in round 1 before the model produces a
 * token (packages/core/ai/runtime/skill_forcing.py). A composer default that
 * quietly fills it makes every unrelated message run the idea skill. These
 * cases execute the real attach block lifted out of EmbeddedChat.tsx.
 */
const attachBlockSource = chatSource.match(
  /const armedIdeaSkill =[\s\S]*?const effectiveManualSkills =[\s\S]*?: \[\];/,
)?.[0];
const builtInSkillsSource = chatSource.match(
  /const IDEA_BUILT_IN_SKILLS: [^=]+= (\{[\s\S]*?\n\});/,
)?.[1];

test("a send attaches a built-in skill only on an explicit user gesture", () => {
  assert.ok(attachBlockSource, "the composer skill-attach block should be findable");
  assert.ok(builtInSkillsSource, "IDEA_BUILT_IN_SKILLS should be findable");

  const builtInSkills = new Function(`return (${builtInSkillsSource});`)();
  // `options` joined the block when workflow sends landed; the lifted code
  // reads options.workflow, so it has to be supplied like any other binding.
  const attachedSkills = new Function(
    "messages",
    "ideaComposer",
    "manualSkills",
    "IDEA_BUILT_IN_SKILLS",
    "options",
    `${attachBlockSource}\nreturn effectiveManualSkills;`,
  );
  const idsFor = (messages, ideaComposer, manualSkills = [], options = {}) =>
    attachedSkills(messages, ideaComposer, manualSkills, builtInSkills, options).map(
      (skill) => skill.id,
    );

  const someHistory = [{ role: "user" }, { role: "assistant" }];
  const defaultFocus = { mode: "new-idea", origin: "default" };

  // The regression this pins: an ordinary follow-up ("regenerate this as a Word
  // doc") must carry no skill at all.
  assert.deepEqual(idsFor(someHistory, defaultFocus), []);
  assert.deepEqual(idsFor(someHistory, null), []);
  // Even an armed mode must not leak into a conversation that already started.
  assert.deepEqual(idsFor(someHistory, { mode: "new-idea", origin: "user" }), []);
  // A workflow send owns the turn: it must not also carry the idea skill.
  assert.deepEqual(
    idsFor([], { mode: "new-idea", origin: "user" }, [], { workflow: "wf_1" }),
    [],
  );
  // The cosmetic empty-state focus is not a request for the skill either.
  assert.deepEqual(idsFor([], defaultFocus), []);

  // Deliberate gestures still work.
  assert.deepEqual(idsFor([], { mode: "new-idea", origin: "user" }), [
    "solo-business-idea-finder",
  ]);
  assert.deepEqual(idsFor([], { mode: "validate-idea", origin: "user" }), [
    "solo-business-idea-review",
  ]);
  // A quick-action card passes its skill explicitly and always wins.
  assert.deepEqual(
    idsFor(someHistory, defaultFocus, [{ id: "solo-business-idea-finder" }]),
    ["solo-business-idea-finder"],
  );
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

test("idea rail actions are accessible, responsive, and motion-safe", async () => {
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
      ".workspace-mode-pill--idea.workspace-mode-pill--active {\n  width: 108px;",
    ),
  );
  assert.ok(
    cssSource.includes(
      ".embedded-chat-body--empty .workspace-mode-pill--idea {\n  width: 104px;\n}",
    ),
  );
  assert.ok(
    !cssSource.includes(
      ".embedded-chat-body--empty .workspace-mode-pill--idea:hover",
    ),
  );
  assert.ok(
    cssSource.includes(
      ".embedded-chat-body--empty .workspace-mode-summary {\n  max-width: min(100%, 600px);\n  margin-top: 12px;",
    ),
  );
  assert.match(
    cssSource,
    /@media \(prefers-reduced-motion: reduce\)[\s\S]*?\.workspace-mode-pill,[\s\S]*?animation: none;/,
  );
  for (const kind of [
    "research",
    "agents",
    "automations",
    "flows",
    "new-idea",
    "validate-idea",
    "workspace",
    "slides",
    "docs",
    "pdf",
    "sheets",
    "website",
    "image",
    "video",
  ]) {
    assert.ok(
      railVisualSource.includes(`"/assets/workspace-rail-v2/${kind}.png"`),
      `missing refined image for ${kind}`,
    );
    await access(
      path.join(webRoot, "public/assets/workspace-rail-v2", `${kind}.png`),
    );
  }
  assert.equal(chatSource.includes("<WorkspaceRailCanvas />"), false);
  assert.ok(railVisualSource.includes("<img"));
  assert.ok(railVisualSource.includes("draggable={false}"));
  assert.ok(railVisualSource.includes("data-kind={kind}"));
  assert.ok(railVisualSource.includes("workspace-rail-v2/new-idea.png"));
  assert.ok(railVisualCssSource.includes("workspace-rail-star-glint"));
  assert.ok(railVisualCssSource.includes("workspace-rail-mechanism-turn"));
  assert.ok(railVisualCssSource.includes("workspace-rail-search-tilt"));
  for (const animationName of [
    "workspace-rail-people-gather",
    "workspace-rail-check-confirm",
    "workspace-rail-window-shuffle",
    "workspace-rail-slide-fan",
    "workspace-rail-document-lift",
    "workspace-rail-pdf-drop",
    "workspace-rail-grid-tap",
    "workspace-rail-site-flow",
    "workspace-rail-image-sway",
    "workspace-rail-video-pulse",
    "workspace-rail-flow-route",
  ]) {
    assert.ok(railVisualCssSource.includes(animationName));
  }
  assert.ok(railVisualCssSource.includes("background: transparent"));
  assert.ok(!railVisualCssSource.includes("mix-blend-mode"));
  assert.ok(railVisualCssSource.includes("prefers-reduced-motion: reduce"));
  assert.ok(chatSource.includes('className="preview-flow-route"'));
  assert.ok(cssSource.includes(".preview-flow-route-node--output"));
  assert.ok(cssSource.includes('html[data-theme="dark"] .preview-flow-route'));
  assert.ok(chatSource.includes("function FlowTemplateSamples()"));
  assert.ok(chatSource.includes("api.workflows.templates()"));
  assert.ok(chatSource.includes("function FlowTemplateScreenshot"));
  assert.ok(chatSource.includes("FLOW_TEMPLATE_PREVIEW_GRAPHS"));
  assert.ok(chatSource.includes("<FlowTemplateScreenshot template={template}"));
  assert.ok(!chatSource.includes("workspace-flow-template-meta"));
  assert.ok(cssSource.includes(".workspace-flow-template-screenshot"));
  assert.ok(cssSource.includes(".workspace-flow-template-node-card"));
  assert.ok(
    cssSource.includes(
      'html[data-theme="dark"]\n  .workspace-sample-preview--flows',
    ),
  );
  assert.equal(cssSource.includes(".workspace-idea-quick-actions"), false);
});

test("PDF samples support preview, paging, and remix", async () => {
  const samples = [
    "solis_editorial_report",
    "wildwood_creative_proposal",
    "gridline_swiss_invoice",
    "civic_ecologies_paper",
  ];
  const files = [
    "solis-editorial-market-report.pdf",
    "wildwood-bold-creative-proposal.pdf",
    "gridline-swiss-studio-invoice.pdf",
    "civic-ecologies-academic-paper.pdf",
  ];

  assert.ok(chatSource.includes('| "pdf"'));
  assert.ok(chatSource.includes('key: "pdf"'));
  assert.ok(chatSource.includes('preview: "pdf"'));
  assert.ok(chatSource.includes("WorkspaceSampleQuickPreview"));
  assert.ok(chatSource.includes("workspace-sample-grid--previewable"));
  // Whatever the remix handler is called this week, it has to reach the
  // composer hand-off — otherwise the Remix button silently stops remixing.
  // Matched by following the wiring rather than by pinning the name, which
  // has already changed twice.
  const remixHandler = chatSource.match(/onRemix=\{(\w+)\}/)?.[1];
  assert.ok(remixHandler, "the preview card must wire an onRemix handler");
  assert.match(
    chatSource,
    new RegExp(`const ${remixHandler} = useCallback\\([\\s\\S]*?onSampleSelect\\(sample`),
    `${remixHandler} must hand the sample to the composer`,
  );
  assert.ok(cssSource.includes(".workspace-sample-quick-preview-modal"));
  assert.ok(cssSource.includes(".workspace-sample-grid--previewable"));
  assert.ok(
    cssSource.includes(
      ".workspace-sample-preview--pdf.workspace-sample-preview--browseable",
    ),
  );
  assert.ok(cssSource.includes("object-position: top center"));

  for (const source of [enSource, zhSource, esSource]) {
    assert.ok(source.includes('"component.embedded_chat.quick_preview"'));
    assert.ok(source.includes('"component.embedded_chat.remix"'));
    for (const sample of samples) {
      assert.ok(
        source.includes(`"component.embedded_chat.side_hustle.pdf.${sample}.title"`),
        `missing PDF sample copy for ${sample}`,
      );
    }
  }

  await Promise.all(
    files.map((file) => access(path.join(webRoot, "public/assets/samples/artifacts/pdf", file))),
  );
});

test("DOCX samples support full-card preview, paging, download, and remix", async () => {
  const templates = [
    {
      id: "shelter_photo_report",
      file: "shelter-photo-report.docx",
      pages: 2,
    },
    {
      id: "adaptive_daylight_experiment",
      file: "adaptive-daylight-experiment-report.docx",
      pages: 2,
    },
    {
      id: "circular_timber_investment_memo",
      file: "circular-timber-investment-memo.docx",
      pages: 1,
    },
    {
      id: "partnership_pilot_letter",
      file: "partnership-pilot-letterhead.docx",
      pages: 1,
    },
  ];

  assert.ok(chatSource.includes('| "docs"'));
  assert.ok(chatSource.includes('| "pdf"'));
  assert.ok(
    chatSource.includes(
      'className="workspace-sample-grid workspace-sample-grid--previewable"',
    ),
  );
  assert.ok(chatSource.includes('t("component.embedded_chat.open_document")'));
  assert.ok(chatSource.includes('capability === "docs"\n            ? detailImageSrcs[0]'));
  assert.match(
    cssSource,
    /\.workspace-sample-preview--docs\.workspace-sample-preview--browseable[\s\S]*?object-fit: cover/,
  );
  const docsBlock = chatSource.match(/docs: \[([\s\S]*?)\n  \],\n  pdf:/)?.[1];
  assert.ok(docsBlock, "Docs sample definitions should be present");
  // The four document templates replaced the legacy Docs samples. The gallery
  // has since grown past them, so this asserts they are still all there rather
  // than pinning the list to exactly four.
  const docsIds = [...docsBlock.matchAll(/id: "([^"]+)"/g)].map((match) => match[1]);
  for (const { id } of templates) {
    assert.ok(docsIds.includes(id), `Docs template ${id} should still be offered`);
  }
  for (const { id, file, pages } of templates) {
    assert.match(
      docsBlock,
      new RegExp(
        `id: "${id}"[\\s\\S]*?sampleSrc: "/assets/samples/artifacts/docs/${file}"[\\s\\S]*?detailPageCount: ${pages}`,
      ),
    );
  }

  for (const source of [enSource, zhSource, esSource]) {
    assert.ok(source.includes('"component.embedded_chat.open_document"'));
    for (const { id } of templates) {
      for (const field of [
        "title",
        "outcome",
        "prompt",
        "label",
        "preview_title",
        "line_1",
        "line_2",
        "chip_1",
        "chip_2",
      ]) {
        assert.ok(
          source.includes(
            `"component.embedded_chat.side_hustle.docs.${id}.${field}"`,
          ),
          `missing DOCX sample copy for ${id}.${field}`,
        );
      }
    }
  }

  await Promise.all(
    templates.map(({ file }) =>
      access(path.join(webRoot, "public/assets/samples/artifacts/docs", file)),
    ),
  );
  await Promise.all(
    templates.flatMap(({ id, pages }) =>
      Array.from({ length: pages }, (_, index) => index + 1).map((page) =>
        access(
          path.join(
            webRoot,
            "public/assets/samples/details/docs",
            `${id}-${String(page).padStart(2, "0")}.png`,
          ),
        ),
      ),
    ),
  );
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
  assert.ok(enSource.includes('"component.embedded_chat.new_idea_today": "Ideas"'));
  assert.ok(zhSource.includes('"component.embedded_chat.new_idea_today": "创意"'));
  assert.ok(esSource.includes('"component.embedded_chat.new_idea_today": "Ideas"'));
  assert.equal(enSource.includes('"OPC business ideas"'), false);
  assert.equal(zhSource.includes('"OPC 一人公司创意"'), false);
  assert.ok(zhSource.includes("先看 4 个种子创意"));
  assert.ok(zhSource.includes("根据我的情况生成全新创意"));
  assert.ok(zhSource.includes("用四个事实说明你的创意"));
  assert.ok(chatSource.includes('variant="primary"'));
  assert.ok(chatSource.includes("onValidationStart();"));
  assert.equal(chatSource.includes("workspace-idea-validation-cta"), false);
  assert.ok(cssSource.includes(".workspace-idea-summary dd"));
  assert.ok(cssSource.includes("font-size: 11px"));
  assert.ok(skillLibrarySource.includes("None of the 18 starters is itself"));
});
