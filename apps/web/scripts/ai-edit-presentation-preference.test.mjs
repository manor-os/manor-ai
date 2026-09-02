#!/usr/bin/env node
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { build } from "esbuild";

const readSource = (path) => readFile(new URL(path, import.meta.url), "utf8");

const [
  preferenceSource,
  accountSource,
  settingsSource,
  editorSource,
  i18nSource,
  styleSource,
] = await Promise.all([
  readSource("../src/lib/aiEditPreferences.ts"),
  readSource("../src/pages/Account.tsx"),
  readSource("../src/pages/Settings.tsx"),
  readSource("../src/pages/DocEditor.tsx"),
  readSource("../src/lib/i18n.ts"),
  readSource("../src/index.css"),
]);
const floatingChatSource = await readSource("../src/components/FloatingChat.tsx");
const editorLiveChatSource = await readSource("../src/lib/editorLiveChat.ts");
const apiSource = await readSource("../src/lib/api.ts");

const preferenceBundle = await build({
  stdin: {
    contents: `export * from "../src/lib/aiEditPreferences.ts";`,
    loader: "tsx",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "esm",
  platform: "browser",
  write: false,
  logLevel: "silent",
});
const preferenceModuleUrl = `data:text/javascript;base64,${Buffer.from(
  preferenceBundle.outputFiles[0].text,
).toString("base64")}`;
const {
  mergeAiEditDisplayPreference,
  resolveAiEditDisplayMode,
} = await import(preferenceModuleUrl);

const localeNames = Array.from(
  i18nSource.matchAll(/import\s+\w+\s+from\s+"\.\/i18n\/([^"/]+)"/g),
  (match) => match[1],
);
const localeSources = await Promise.all(
  localeNames.map((name) => readSource(`../src/lib/i18n/${name}.ts`)),
);

test("AI Edit display defaults to live editing and rejects malformed preferences", () => {
  assert.match(preferenceSource, /enum AiEditDisplayMode/);
  assert.match(preferenceSource, /Live = "live"/);
  assert.match(preferenceSource, /InstantPreview = "instant_preview"/);
  assert.match(preferenceSource, /DEFAULT_AI_EDIT_DISPLAY_MODE = AiEditDisplayMode\.Live/);
  assert.match(preferenceSource, /if \(!isRecord\(preferences\)\) return DEFAULT_AI_EDIT_DISPLAY_MODE/);
  assert.match(preferenceSource, /const nested = preferences\.preferences/);
  assert.match(preferenceSource, /mergeAiEditDisplayPreference/);
  assert.equal(resolveAiEditDisplayMode(undefined), "live");
  assert.equal(resolveAiEditDisplayMode({ ai_edit_display_mode: "instant_preview" }), "instant_preview");
  assert.equal(resolveAiEditDisplayMode({ ai_edit_presentation_mode: "instant_preview" }), "instant_preview");
  assert.equal(resolveAiEditDisplayMode({ ai_edit_display_mode: "unknown", ai_edit_presentation_mode: "instant_preview" }), "instant_preview");
  assert.equal(resolveAiEditDisplayMode({ preferences: { ai_edit_display_mode: "instant_preview" } }), "instant_preview");
  assert.equal(resolveAiEditDisplayMode({ ai_edit_display_mode: "unknown" }), "live");
  assert.deepEqual(
    mergeAiEditDisplayPreference({ locale: "zh", preferences: { density: "compact" } }, "instant_preview"),
    {
      locale: "zh",
      ai_edit_display_mode: "instant_preview",
      preferences: { density: "compact", ai_edit_display_mode: "instant_preview" },
    },
  );
});

test("Appearance settings persist the personal AI Edit display preference", () => {
  assert.match(settingsSource, /if \(tab === "appearance"\) return <AppearanceSection embedded \/>/);
  assert.match(settingsSource, /description: "Theme, AI Edit display, and interface preferences\."/);
  assert.match(accountSource, /queryKey: \["preferences"\]/);
  assert.match(accountSource, /api\.admin\.updatePreferences\(\{[\s\S]*?\[AI_EDIT_DISPLAY_PREFERENCE_KEY\]: mode/);
  assert.match(accountSource, /mergeAiEditDisplayPreference\(current, mode\)/);
  assert.match(accountSource, /value: AiEditDisplayMode\.Live/);
  assert.match(accountSource, /value: AiEditDisplayMode\.InstantPreview/);
  assert.match(accountSource, /role="radiogroup"[\s\S]*?account-ai-edit-grid/);
  assert.match(styleSource, /\.account-theme-card:focus-visible/);
  assert.match(styleSource, /\.account-ai-edit-card\.is-active \.account-ai-edit-preview\[data-ai-edit-preview="live"\][\s\S]*?animation: account-ai-edit-caret/);
});

test("DocEditor honors instant preview without changing Accept and Discard semantics", () => {
  assert.match(editorSource, /const aiEditDisplayMode = resolveAiEditDisplayMode\(aiEditPreferences\)/);
  assert.match(
    editorSource,
    /meta\.streamEvent === AiEditPatchStreamEventKind\.Delta[\s\S]*?aiEditDisplayMode === AiEditDisplayMode\.InstantPreview[\s\S]*?return false/,
  );
  assert.match(editorSource, /if \(aiEditDisplayMode === AiEditDisplayMode\.InstantPreview\)[\s\S]*?current: nextText[\s\S]*?status: AiEditPreviewStatus\.Animating[\s\S]*?setLiveDiff\(null\)/);
  assert.match(editorSource, /const completeEditorLiveContent = useCallback[\s\S]*?status: AiEditPreviewStatus\.Ready/);
  assert.match(editorSource, /const allFrames = buildEditorLiveTextFrames\(before, nextText/);
  assert.match(editorSource, /contentRef\.current = preview\.baseline/);
  assert.match(editorSource, /await saveMutation\.mutateAsync\(request\)[\s\S]*?liveEditPreviewRef\.current = null/);
  assert.match(editorSource, /rollback: discardLiveEditPreview/);
});

test("PowerPoint AI edits stay as structured previews until Accept", () => {
  const previewStart = editorSource.indexOf("const previewPresentationLiveEdit = useCallback");
  const previewEnd = editorSource.indexOf("const applyGeneratedPresentationImage", previewStart);
  const previewSource = editorSource.slice(previewStart, previewEnd);
  assert.ok(previewStart >= 0 && previewEnd > previewStart);
  assert.match(previewSource, /baselineSlides/);
  assert.match(previewSource, /currentSlides/);
  assert.match(previewSource, /setPptxSlides\(currentSlides\)/);
  assert.doesNotMatch(previewSource, /schedulePresentationSave/);

  const discardStart = editorSource.indexOf("const discardLiveEditPreview = useCallback");
  const acceptStart = editorSource.indexOf("const acceptLiveEditPreview = useCallback", discardStart);
  const applyStart = editorSource.indexOf("const applyEditorLiveContent = useCallback", acceptStart);
  const discardSource = editorSource.slice(discardStart, acceptStart);
  const acceptSource = editorSource.slice(acceptStart, applyStart);
  assert.match(discardSource, /preview\.mode === "presentation" && preview\.baselineSlides/);
  assert.match(discardSource, /setPptxSlides\(baselineSlides\)/);
  assert.doesNotMatch(discardSource, /schedulePresentationSave/);
  assert.match(acceptSource, /preview\.mode === "presentation" && preview\.currentSlides/);
  assert.match(acceptSource, /buildPresentationSaveRequest\(acceptedSlides, revision\)[\s\S]*?await presentationSaveMutation\.mutateAsync\(request\)/);
  assert.ok(
    acceptSource.indexOf("liveEditPreviewRef.current = null")
      > acceptSource.indexOf("await saveMutation.mutateAsync(request)"),
    "Accept must keep preview mode until the text save is confirmed",
  );
  assert.match(
    acceptSource,
    /if \(codeWorkspace\.commitFileContent[\s\S]*?persisted = await codeWorkspace\.saveActive\(\)/,
  );

  const generatedImageStart = editorSource.indexOf("const applyGeneratedPresentationImage");
  const generatedImageEnd = editorSource.indexOf("const getEditorLiveContent", generatedImageStart);
  const generatedImageSource = editorSource.slice(generatedImageStart, generatedImageEnd);
  assert.match(generatedImageSource, /previewPresentationLiveEdit\(nextSlides,[\s\S]*?meta\)/);
  assert.doesNotMatch(generatedImageSource, /handleSlidesChange\(nextSlides\)/);
});

test("PowerPoint native AI patches reload the approved persisted PPTX", () => {
  const nativeReloadStart = editorSource.indexOf("const applyNativePresentationFilePatch");
  const nativeReloadEnd = editorSource.indexOf("const getEditorLiveContent", nativeReloadStart);
  const nativeReloadSource = editorSource.slice(nativeReloadStart, nativeReloadEnd);
  assert.ok(nativeReloadStart >= 0 && nativeReloadEnd > nativeReloadStart);
  assert.match(nativeReloadSource, /result\.document_id && result\.document_id !== docId/);
  assert.match(nativeReloadSource, /result\.path !== doc\.fs_path/);
  assert.match(nativeReloadSource, /api\.documents\.previewBlob\(docId, \{[\s\S]*?cache: false,[\s\S]*?force: true,[\s\S]*?signal: meta\.signal/);
  assert.match(nativeReloadSource, /sha256Hex\(buffer\)/);
  assert.match(nativeReloadSource, /downloadedSha256 !== result\.source_sha256\.toLowerCase\(\)/);
  assert.match(nativeReloadSource, /parsePptxForEditor\(buffer/);
  assert.match(nativeReloadSource, /pptxOriginalBufferRef\.current = buffer\.slice\(0\)/);
  assert.match(nativeReloadSource, /pptxBaselineSlidesRef\.current = structuredClone\(nextSlides\)/);
  assert.match(nativeReloadSource, /pptxSlidesRef\.current = nextSlides/);
  assert.match(nativeReloadSource, /liveEditPreviewRef\.current = null/);
  assert.match(nativeReloadSource, /invalidateKnowledgeQueries\(queryClient\)/);
  assert.match(editorSource, /loadOfficeEditSource\(docId, needsLegacyOfficeConversion\)/);
  assert.match(editorSource, /headers\.get\("X-Manor-Source-SHA256"\)/);
  assert.match(editorSource, /expectedSourceSha256 = request\.expectedSourceSha256/);
  assert.match(editorSource, /requireCurrentAuthToken\(\),[\s\S]*?expectedSourceSha256/);
  assert.match(apiSource, /form\.append\("expected_source_sha256", expectedSourceSha256\)/);

  assert.match(editorSource, /sourcePath: mode === "code" \? undefined : doc\?\.fs_path/);
  assert.match(editorLiveChatSource, /routePath: detail\.routePath \|\| window\.location\.pathname/);
  assert.match(floatingChatSource, /const routePath = editorLiveInfo\?\.routePath/);
  assert.doesNotMatch(floatingChatSource, /const sourcePath = editorLiveInfo\?\.sourcePath/);
  assert.match(editorSource, /supportsNativeFilePatch: Boolean\(doc\?\.fs_path\)/);
  assert.match(editorSource, /applyNativeFilePatch: applyNativePresentationFilePatch/);
  assert.match(floatingChatSource, /supportsNativeFilePatch: Boolean\([\s\S]*?applyNativeFilePatch/);
  assert.match(editorLiveChatSource, /function nativeFilePatchResultFromSseFrame/);
  assert.match(editorLiveChatSource, /record\.patched !== true/);
  assert.match(floatingChatSource, /nativeFilePatchResultFromSseFrame,/);
  assert.match(floatingChatSource, /await detail\.applyNativeFilePatch\(result/);
  assert.match(floatingChatSource, /persistedNativePatchKey && reloadedNativePatchKey/);
  assert.match(floatingChatSource, /stage: "persisted bytes reloaded"/);
  const approvalStart = floatingChatSource.indexOf("const handleHITLAction = useCallback");
  const approvalEnd = floatingChatSource.indexOf("const handleRetryMessage", approvalStart);
  const approvalSource = floatingChatSource.slice(approvalStart, approvalEnd);
  assert.match(approvalSource, /targetHitl\?\.tool === "patch_file"/);
  assert.match(approvalSource, /pipeEditorLiveEditStream\(/);
  assert.match(approvalSource, /applyNativeFilePatch/);
  assert.match(approvalSource, /api\.chat\.stream\(hitlMessage, currentConvId/);

  const retryStart = floatingChatSource.indexOf("const handleRetryMessage = useCallback");
  const retryEnd = floatingChatSource.indexOf("const feedbackKeyForMessage", retryStart);
  const retrySource = floatingChatSource.slice(retryStart, retryEnd);
  assert.match(retrySource, /if \(editorLiveSessionActive\)/);
  assert.match(retrySource, /await handleSend\(retryRequest\.message, \[\], \[\], null\)/);
  assert.ok(
    retrySource.indexOf("await handleSend(retryRequest.message, [], [], null)")
      < retrySource.indexOf("api.chat.stream("),
  );
});

test("AI Edit display choices are translated in every registered locale", () => {
  const keys = [
    "ai_edit_display",
    "ai_edit_display_description",
    "ai_edit_live_label",
    "ai_edit_live_description",
    "ai_edit_instant_label",
    "ai_edit_instant_description",
    "ai_edit_preference_failed",
  ];
  const editorKeys = [
    "ai_edit_preview_controls",
    "ai_edit_pending_change",
    "ai_edit_pending_changes",
    "ai_edit_selecting",
    "ai_edit_deleting",
    "ai_edit_formatting",
    "ai_edit_typing",
    "ai_edit_review_preview",
    "ai_edit_editing_document",
    "ai_edit_back_to_editor",
    "ai_edit_review",
    "ai_edit_discard",
    "ai_edit_accept",
    "ai_edit_discarded",
    "ai_edit_accepted",
    "ai_edit_save_blocked",
    "ai_edit_updated",
  ];
  for (const source of localeSources) {
    for (const key of keys) {
      assert.match(source, new RegExp(`"page\\.account\\.${key}":`));
    }
    for (const key of editorKeys) {
      assert.match(source, new RegExp(`"page\\.doc_editor\\.${key}":`));
    }
  }
  assert.doesNotMatch(editorSource, /Review this temporary preview|AI preview discarded|AI edit preview controls/);
});
