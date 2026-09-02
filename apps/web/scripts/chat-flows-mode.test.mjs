#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const selectorSource = await readFile(
  new URL("../src/components/ChatModeSelector.tsx", import.meta.url),
  "utf8",
);
const briefSource = await readFile(
  new URL("../src/components/ChatModeBriefPanel.tsx", import.meta.url),
  "utf8",
);
const toolbarSource = await readFile(
  new URL("../src/components/ChatModeToolbar.tsx", import.meta.url),
  "utf8",
);
const englishSource = await readFile(
  new URL("../src/lib/i18n/en.ts", import.meta.url),
  "utf8",
);
const chineseSource = await readFile(
  new URL("../src/lib/i18n/zh.ts", import.meta.url),
  "utf8",
);
const embeddedChatSource = await readFile(
  new URL("../src/components/EmbeddedChat.tsx", import.meta.url),
  "utf8",
);
const previewAccessSource = await readFile(
  new URL("../src/lib/previewFeatureAccess.ts", import.meta.url),
  "utf8",
);
const appLayoutSource = await readFile(
  new URL("../src/layouts/AppLayout.tsx", import.meta.url),
  "utf8",
);
const contentTypeIconsSource = await readFile(
  new URL("../src/components/contentTypeIcons.ts", import.meta.url),
  "utf8",
);
test("global chat exposes an explicit Flows mode", () => {
  assert.match(selectorSource, /\| "flows"/);
  assert.match(selectorSource, /key: "flows"/);
  assert.match(selectorSource, /component\.chat_mode\.flows/);
  assert.match(briefSource, /flows:/);
  assert.match(englishSource, /"component\.chat_mode\.flows": "Flows"/);
  assert.match(chineseSource, /"component\.chat_mode\.flows": "Flows"/);
});

test("Flows mode stays visible but disabled while the feature is coming soon", () => {
  assert.match(selectorSource, /usePreviewFeatureAccess\("flows"\)/);
  assert.match(selectorSource, /mode\.key === "flows" && !flowsAccess\.enabled/);
  assert.match(selectorSource, /!flowsAccess\.released/);
  assert.match(selectorSource, /disabled=\{unavailable\}/);
  assert.match(selectorSource, /component\.chat_mode\.soon/);
  assert.match(englishSource, /"component\.chat_mode\.flows_coming_soon": "Flows are coming soon"/);
  assert.match(chineseSource, /"component\.chat_mode\.flows_coming_soon": "Flows 即将推出"/);
  assert.match(embeddedChatSource, /const flowsAccess = usePreviewFeatureAccess\("flows"\)/);
  assert.match(embeddedChatSource, /railKey === "flows" && !flowsAccess\.enabled/);
  assert.match(embeddedChatSource, /workspace-mode-pill--disabled/);
  assert.match(embeddedChatSource, /disabled=\{unavailable\}/);
  assert.match(previewAccessSource, /if \(feature === "flows"\) return state\.flows_released/);
  assert.match(previewAccessSource, /released,/);
  assert.match(appLayoutSource, /flowsConfigurationItem\(flowsAvailable\)/);
});

test("chat mode icons match the work they represent", () => {
  assert.match(selectorSource, /key: "pdf",\s+icon: CONTENT_TYPE_ICONS\.pdf/);
  assert.match(selectorSource, /key: "slides",\s+icon: CONTENT_TYPE_ICONS\.slides/);
  assert.match(selectorSource, /key: "sheet",\s+icon: CONTENT_TYPE_ICONS\.sheet/);
  assert.match(selectorSource, /key: "website",\s+icon: CONTENT_TYPE_ICONS\.website/);
  assert.match(selectorSource, /key: "research",\s+icon: IconSearch/);
  assert.doesNotMatch(selectorSource, /key: "website",\s+icon: IconWorkspace/);
  assert.match(contentTypeIconsSource, /pdf: IconReport/);
  assert.match(contentTypeIconsSource, /slides: IconPresentation/);
  assert.match(contentTypeIconsSource, /sheet: IconExcelGrid/);
});

test("Video chat mode exposes auto, coded-motion, and AI-video generation paths", () => {
  assert.match(briefSource, /generation_mode:\s*VideoGenerationMode\.AUTO/);
  assert.match(briefSource, /VideoGenerationMode\.NATIVE_MOTION/);
  assert.match(briefSource, /VideoGenerationMode\.AI_VIDEO/);
  assert.match(toolbarSource, /videoGenerationOptions/);
  assert.match(toolbarSource, /component\.chat_mode\.brief_generation/);
  assert.match(englishSource, /"component\.chat_mode\.generation_native"/);
  assert.match(chineseSource, /"component\.chat_mode\.generation_ai"/);
});

test("explicit Flows mode uses real server templates in a new chat", () => {
  // A new chat in auto mode re-focuses the idea card; an explicit chat mode
  // (Flows, Docs, …) clears it. Either way the re-arm is origin "default", so it
  // only changes what the empty state looks like — never what gets sent.
  assert.match(
    embeddedChatSource,
    /setIdeaComposer\(\s*chatModeRef\.current === "auto"\s*\? \{ mode: "new-idea", origin: "default" \}\s*: null,\s*\)/,
  );
  assert.match(
    embeddedChatSource,
    /messages\.length === 0 && ideaComposer\?\.origin === "user"/,
  );
  assert.match(
    embeddedChatSource,
    /placeholder=\{\s*requestChatMode\s*\? getChatModeInputPlaceholder/,
  );
  assert.match(
    embeddedChatSource,
    /messages\.length === 0\s*&&\s*\(\s*<WorkspaceWelcome/,
  );
  assert.match(embeddedChatSource, /key: "flows"/);
  assert.match(embeddedChatSource, /"flows",\s*"new-idea"/);
  assert.match(embeddedChatSource, /workspace-sample-card--flow-template/);
  assert.match(embeddedChatSource, /api\.workflows\.templates\(\)/);
  assert.match(embeddedChatSource, /api\.workflows\.installTemplate\(template\.id\)/);
  assert.match(embeddedChatSource, /opc-generate-topic-from-knowledge-v1/);
  assert.match(embeddedChatSource, /opc-write-article-from-topic-v1/);
  assert.match(embeddedChatSource, /opc-create-image-from-topic-v1/);
  assert.match(embeddedChatSource, /opc-create-video-from-topic-v1/);
  assert.match(embeddedChatSource, /\.slice\(0, 4\)/);
  assert.match(embeddedChatSource, /\/flows\?workflow=/);
  assert.match(embeddedChatSource, /function FlowTemplateQuickPreview/);
  assert.match(embeddedChatSource, /function FlowTemplateScreenshot/);
  assert.match(
    embeddedChatSource,
    /FLOW_TEMPLATE_PREVIEW_GRAPHS: Record<string, FlowTemplatePreviewGraph>/,
  );
  assert.match(embeddedChatSource, /<FlowTemplateScreenshot template=\{template\} \/>/);
  assert.match(embeddedChatSource, /label: "Workspace knowledge"/);
  assert.match(embeddedChatSource, /label: "Generate image"/);
  assert.match(embeddedChatSource, /label: "Generate video"/);
  assert.match(
    embeddedChatSource,
    /workspace-flow-template-preview-details/,
  );
  assert.doesNotMatch(embeddedChatSource, /workspace-flow-template-meta/);
  assert.match(embeddedChatSource, /<p>\{template\.description\}<\/p>/);
  assert.match(embeddedChatSource, /template\.node_count/);
  assert.match(embeddedChatSource, /template\.version/);
  assert.match(
    embeddedChatSource,
    /component\.embedded_chat\.flow_templates\.installed_status/,
  );
  assert.match(
    embeddedChatSource,
    /setPreviewTemplate\(template\)/,
  );
  assert.match(
    embeddedChatSource,
    /component\.embedded_chat\.quick_preview/,
  );
  assert.match(embeddedChatSource, /component\.embedded_chat\.remix/);
  assert.match(
    embeddedChatSource,
    /component\.embedded_chat\.flow_templates\.install_template/,
  );
  assert.doesNotMatch(embeddedChatSource, /title: "Lead qualification flow"/);
  assert.doesNotMatch(embeddedChatSource, /title: "Weekly metrics flow"/);
  assert.match(
    chineseSource,
    /"component\.embedded_chat\.flow_templates\.install_template": "安装模板"/,
  );
  assert.match(embeddedChatSource, /activeIdeaMode=\{ideaComposerMode\}/);
});

test("every chat mode refreshes its persisted response after streaming", () => {
  assert.match(
    embeddedChatSource,
    /const completedSessionKey = await startStream\(/,
  );
  assert.match(
    embeddedChatSource,
    /await loadConversationMessages\(completedConversationId,[\s\S]*allowSettledStreamRefresh: true/,
  );
  assert.doesNotMatch(embeddedChatSource, /turnChatMode === "flows"[\s\S]*loadConversationMessages\(completedConversationId/);
});
