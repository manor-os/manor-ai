import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const editorSource = await readFile(
  new URL("../src/pages/DocEditor.tsx", import.meta.url),
  "utf8",
);
const sitePublishSource = await readFile(
  new URL("../src/components/SitePublishAction.tsx", import.meta.url),
  "utf8",
);
const viewerSource = await readFile(
  new URL("../src/pages/FileViewer.tsx", import.meta.url),
  "utf8",
);

test("manual document saves report the persisted result", () => {
  assert.match(editorSource, /import \{ useToastStore \} from "\.\.\/stores\/toast"/);
  assert.match(
    editorSource,
    /const handleManualSave = useCallback\(async \(\) => \{[\s\S]*?await flushSave\(content\)[\s\S]*?showSaveSuccess\(t\("page\.blueprint_detail\.saved"\)\)[\s\S]*?showSaveError\(t\("page\.blueprint_detail\.save_failed"\)\)/,
  );
  assert.match(editorSource, /onClick=\{\(\) => void handleManualSave\(\)\}/);
});

test("saved feedback appears only after persistence and then clears", () => {
  assert.match(
    editorSource,
    /displayedSaveStatus === "saved" && previous\.status !== "saved"/,
  );
  assert.match(editorSource, /const savedFeedbackScope = mode === "code" \? `\$\{docId \|\| ""\}:\$\{activeCodePath\}` : docId \|\| ""/);
  assert.match(editorSource, /!previous \|\| previous\.scope !== savedFeedbackScope/);
  assert.match(
    editorSource,
    /setSavedFeedbackVisible\(true\)[\s\S]*?setTimeout\(\(\) => \{[\s\S]*?setSavedFeedbackVisible\(false\);[\s\S]*?\}, 1600\)/,
  );
  assert.match(
    editorSource,
    /displayedSaveStatus !== "saved" \|\| savedFeedbackVisible/,
  );
  assert.doesNotMatch(
    editorSource,
    /className="doc-editor-footer-status"[\s\S]{0,500}?displayedSaveStatus === "saved"/,
  );
});

test("DOCX editing exposes safe text controls without duplicate format badges", () => {
  assert.match(editorSource, /t\("page\.doc_editor\.mode_word_document"\)/);
  assert.doesNotMatch(editorSource, /<StatusBadge type="gray">\{extLabel\(docName\)\}<\/StatusBadge>/);
  assert.doesNotMatch(editorSource, /docxFidelityMode/);
  assert.doesNotMatch(editorSource, /pointerEvents: "none", opacity: 0\.62/);
  assert.doesNotMatch(editorSource, /docxCompatibilityMode/);
  assert.match(editorSource, /editDocumentFileWithSnapshot\([\s\S]*?docxOriginalBuffer,[\s\S]*?docxBaselineHtml,[\s\S]*?text,[\s\S]*?documentName,/);
  assert.match(editorSource, /contentEditable=\{canEditCurrentDoc && liveEditPreview\?\.mode !== "richtext"\}[\s\S]*?role="textbox"[\s\S]*?aria-multiline="true"/);
});

test("the HTML editor publishes only after saving the whole code project", () => {
  assert.match(editorSource, /import SitePublishAction from "\.\.\/components\/SitePublishAction"/);
  assert.match(
    editorSource,
    /const saveCodeWorkspaceForPublish = useCallback\(async \(\) => \{[\s\S]*?if \(!ensureNoLiveEditPreview\(\)\) return false;[\s\S]*?return codeWorkspace\.saveAll\(\);/,
  );
  assert.match(
    editorSource,
    /canEditCurrentDoc && mode === "code" && isHtmlFile\(docName\)[\s\S]*?<SitePublishAction doc=\{doc \|\| null\} beforePublish=\{saveCodeWorkspaceForPublish\} \/>/,
  );
  assert.match(editorSource, /textSaveQueueRef\.current\.catch\(\(\) => undefined\)\.then/);
  assert.match(editorSource, /!hadPendingTimer && saveStatus === "saved"/);
  const saveBeforePublish = sitePublishSource.indexOf("await beforePublish()");
  const refreshPublishPlan = sitePublishSource.indexOf("const latest = await refresh()", saveBeforePublish);
  assert.ok(saveBeforePublish >= 0, "site publishing should await the editor save hook");
  assert.ok(
    refreshPublishPlan > saveBeforePublish,
    "the publish plan must be refreshed after the latest code is saved",
  );
  assert.match(
    sitePublishSource.slice(refreshPublishPlan),
    /const confirmed: ConfirmedPublish = \{[\s\S]*?snapshotHash: latest\.publish_snapshot_hash,[\s\S]*?primaryAction:[\s\S]*?publishConfirmed\(confirmed\)/,
    "the confirmation action must publish the snapshot loaded after saving",
  );
  assert.match(sitePublishSource, /const publishConfirmed = async \(confirmed: ConfirmedPublish\)[\s\S]*?await api\.sites\.publish\(/);
  assert.match(sitePublishSource, /expected_snapshot_hash: confirmed\.snapshotHash/);
});

test("text saves stay scoped to their document session and latest edit", () => {
  assert.match(editorSource, /type TextSaveRequest = \{[\s\S]*?documentId: string;[\s\S]*?sessionRevision: number;[\s\S]*?editRevision: number;/);
  assert.match(
    editorSource,
    /api\.documents\.replaceFile\(\s*documentId,\s*file,\s*saveIntent,\s*requireCurrentAuthToken\(\),/,
  );
  assert.match(
    editorSource,
    /api\.documents\.saveContent\(\s*documentId,\s*text,\s*saveIntent,\s*requireCurrentAuthToken\(\),/,
  );
  assert.match(
    editorSource,
    /const pendingTextSave = pendingTextSaveRef\.current;[\s\S]*?textSaveMutateRef\.current\(pendingTextSave\);[\s\S]*?textSaveSessionRevisionRef\.current \+= 1/,
  );
  assert.match(
    editorSource,
    /if \(request\.editRevision === textSaveEditRevisionRef\.current\) setSaveStatus\("saved"\)/,
  );
  assert.match(editorSource, /retryDocumentSave\(async \(\) => \{/);
  assert.doesNotMatch(editorSource, /retry: 2/);
  assert.match(editorSource, /showSaveError\(request\.documentName/);
});

test("spreadsheet and presentation saves keep their original document identity", () => {
  assert.match(editorSource, /type SpreadsheetSaveRequest = \{[\s\S]*?documentId: string;[\s\S]*?sessionRevision: number;/);
  assert.match(editorSource, /type PresentationSaveRequest = \{[\s\S]*?documentId: string;[\s\S]*?sessionRevision: number;/);
  assert.match(
    editorSource,
    /api\.documents\.replaceFile\(\s*request\.documentId,\s*file,\s*saveIntent,\s*requireCurrentAuthToken\(\),/,
  );
  assert.match(
    editorSource,
    /const pendingSpreadsheetSave = pendingSpreadsheetSaveRef\.current;[\s\S]*?spreadsheetSaveMutateRef\.current\(pendingSpreadsheetSave\)/,
  );
  assert.match(
    editorSource,
    /const pendingPresentationSave = pendingPresentationSaveRef\.current;[\s\S]*?presentationSaveMutateRef\.current\(pendingPresentationSave\)/,
  );
});

test("text saves wait for byte-format hydration and remain writable", () => {
  assert.match(editorSource, /textFormatLoad: Promise<TextFileSaveSnapshot \| null> \| null;/);
  assert.match(editorSource, /const loadedTextFormat = await request\.textFormatLoad;/);
  assert.match(editorSource, /const savedTextFormat = textFileFormatForSave\(textFormat\)/);
  assert.doesNotMatch(editorSource, /requiresTextFormat|saving is disabled to protect its encoding/);
  assert.match(editorSource, /await api\.documents\.saveContent\(/);
  assert.match(editorSource, /textFormatLoadRef\.current = \{ documentId: docId, sessionRevision, promise: loadPromise \};/);
  assert.match(
    editorSource,
    /needsTextByteHydration[\s\S]*?textBytesReadyDocumentId !== docId/,
  );
});

test("fallback content hydration preserves unsaved local edits", () => {
  assert.match(
    editorSource,
    /const hasUnsavedLocalChange = textSaveEditRevisionRef\.current !== textSavePersistedRevisionRef\.current[\s\S]*?if \(hasUnsavedLocalChange\) return;/,
  );
});

test("site publishing is available only to document editors", () => {
  assert.match(
    viewerSource,
    /!isTaskOutputPreview && canEditCurrentDoc && category === "html"[\s\S]*?<SitePublishAction doc=\{doc\} \/>/,
  );
});

test("site publishing only auto-connects when the plan grants Workspace management", () => {
  assert.match(
    sitePublishSource,
    /const autoConnect = Boolean\(plan\?\.eligible && plan\.can_auto_connect\)/,
  );
  assert.match(sitePublishSource, /auto_connect: confirmed\.autoConnect/);
  assert.doesNotMatch(sitePublishSource, /auto_connect: true/);
});

test("an existing site remains manageable when its source is no longer publishable", () => {
  assert.match(
    sitePublishSource,
    /if \(!checked \|\| \(!target && !site\) \|\| \(!hostingConfigured && !site\)\) return null/,
  );
  assert.match(sitePublishSource, /canRepublish=\{!!target\}/);
  assert.match(
    sitePublishSource,
    /\{canRepublish && \([\s\S]*?sites\.republish/,
  );
  assert.match(
    sitePublishSource,
    /site\.status === "active"[\s\S]*?: canRepublish \? \([\s\S]*?sites\.putOnline/,
  );
});

test("site lookup ignores responses from a previously opened document", () => {
  assert.match(sitePublishSource, /const refreshRequestRef = useRef\(0\)/);
  assert.match(
    sitePublishSource,
    /const requestId = \+\+refreshRequestRef\.current[\s\S]*?await api\.sites\.forPath\(docPath\)[\s\S]*?requestId !== refreshRequestRef\.current/,
  );
  assert.match(
    sitePublishSource,
    /return \(\) => \{[\s\S]*?refreshRequestRef\.current \+= 1/,
  );
});
