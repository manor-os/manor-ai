import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const readSource = (path) => readFile(new URL(path, import.meta.url), "utf8");

const editorSource = await readSource("../src/pages/DocEditor.tsx");
const viewerSource = await readSource("../src/pages/FileViewer.tsx");
const floatingChatSource = await readSource("../src/components/FloatingChat.tsx");
const floatingPanelSource = await readSource("../src/components/FloatingPanel.tsx");
const apiSource = await readSource("../src/lib/api.ts");
const editorLiveChatSource = await readSource("../src/lib/editorLiveChat.ts");
const documentPatchSource = await readSource("../src/lib/documentOoxml.ts");
const spreadsheetPatchSource = await readSource("../src/lib/spreadsheetOoxml.ts");
const presentationPatchSource = await readSource("../src/lib/presentationOoxmlPatch.ts");
const presentationGeometrySource = await readSource("../src/lib/presentationPresetGeometry.ts");
const sanitizerSource = await readSource("../src/lib/sanitizeDocumentHtml.ts");
const indexCssSource = await readSource("../src/index.css");
const i18nSource = await readSource("../src/lib/i18n.ts");
const localeNames = Array.from(
  i18nSource.matchAll(/import\s+\w+\s+from\s+"\.\/i18n\/([^"/]+)"/g),
  (match) => match[1],
);
const localeSources = await Promise.all(localeNames.map((name) => readSource(`../src/lib/i18n/${name}.ts`)));
const editorModeTranslationKeys = [
  "word_document",
  "presentation",
  "richtext",
  "markdown",
  "text",
  "spreadsheet",
  "diagram",
  "code",
];

test("Office editor UI does not expose implementation modes", () => {
  const userFacingSources = [
    editorSource,
    documentPatchSource,
    spreadsheetPatchSource,
    presentationPatchSource,
    ...localeSources,
  ].join("\n");

  assert.doesNotMatch(userFacingSources, /OOXML fidelity|OOXML 保真|Fidelidad OOXML|轻量编辑|Lightweight edit/i);
  assert.doesNotMatch(editorSource, /t\("page\.doc_editor\.(?:docx|xlsx|pptx)_fidelity/);
  assert.doesNotMatch(editorSource, /docx_imported_as_html_saves_as_text/);
  assert.doesNotMatch(userFacingSources, /fidelity mode/i);
  assert.doesNotMatch(editorSource, /useServerBg|pptx_view_mode|pptx_preview_mode|pptx_edit_mode/);
});

test("PPTX edit and presentation views share the structured object renderer", () => {
  assert.doesNotMatch(editorSource, /const hasServerBg|presentation-editor-present-server-image/);
  assert.match(editorSource, /function pptxShapeVisualStyle\(shape: PptxShape\)/);
  assert.match(editorSource, /function pptxParagraphVisualStyle\(paragraph: PptxTextRun, slide: PptxSlide, shape: PptxShape\)/);
  assert.match(editorSource, /pptxParagraphVisualStyle\(paragraph, slide, shape\)/);
  assert.match(editorSource, /pptxParagraphVisualStyle\(t, activeSlide, shape\)/);
  assert.match(editorSource, /whiteSpace: shape\.wordWrap === false \? "pre" : "pre-wrap"/);
  assert.match(editorSource, /function PptxParagraphContent\(/);
  assert.match(editorSource, /function reconcilePptxTextRuns\([\s\S]*?paragraph: PptxTextRun,[\s\S]*?nextText: string,[\s\S]*?sourceMap\?: PresentationTextSourceMap/);
  assert.match(editorSource, /contentEditable\s+suppressContentEditableWarning/);
  assert.match(editorSource, /runs: reconcilePptxTextRuns\(paragraph, newText, nextSourceMap\)/);
  assert.match(editorSource, /onBeforeInput=\{\(event\) => \{[\s\S]*?pptxContentEditableSelection\(event\.currentTarget\)/);
  assert.match(editorSource, /const nextSourceMap = sourceMap[\s\S]*?reconcilePresentationTextSourceMap\(paragraph\.text, newText, paragraph\.sourceMap\)/);
  assert.doesNotMatch(editorSource, /text: newText, runs: undefined/);
  assert.match(editorSource, /const updateImageFit = useCallback\(async \(shapeId: string, imageFit: "cover" \| "contain" \| "fill"\)/);
  assert.match(editorSource, /const imageFitRequestRef = useRef\(new Map<string, number>\(\)\)/);
  assert.match(editorSource, /imageFitRequestRef\.current\.get\(shapeId\) !== requestRevision/);
  assert.match(editorSource, /currentShape\?\.imgUrl !== requestedImageUrl/);
  assert.match(editorSource, /imgCrop: \{\s*l: horizontalCrop,\s*t: verticalCrop,\s*r: horizontalCrop,\s*b: verticalCrop/);
  assert.match(editorSource, /selectedShape\.type === "image" && \(\s*<label className="presentation-editor-format-slider">/);
  assert.match(editorSource, /shape\.imageFit = shape\.imgCrop \? "cover" : "fill"/);
  assert.match(editorSource, /presentationColorWithAlpha\(rc, rPr\)/);
  assert.match(editorSource, /strikethrough: firstStrike, baseline: firstBaseline, spacing: firstSpacing/);
  assert.match(editorSource, /pptxDecodeText\(token\.replace/);
  assert.match(presentationPatchSource, /presentationColorValue\(value: string \| undefined/);
  assert.match(presentationPatchSource, /<a:alphaModFix amt=/);
  assert.match(presentationPatchSource, /<a:br\/>/);
  assert.match(editorSource, /function PptxReadOnlySlide\(\{[\s\S]*?slide,[\s\S]*?thumbnail = false,[\s\S]*?slide: PptxSlide;[\s\S]*?thumbnail\?: boolean;/);
  assert.match(editorSource, /function PptxShapeGeometry\(\{ shape \}: \{ shape: PptxShape \}\)/);
  assert.match(editorSource, /presentationPresetPolygonPoints,[\s\S]*?from "\.\.\/lib\/presentationPresetGeometry"/);
  assert.match(presentationGeometrySource, /export function presentationPresetPolygonPoints\(preset\?: string\)/);
  assert.match(presentationGeometrySource, /rightArrow: \[\[0, 25\]/);
  assert.match(editorSource, /type: "graphic", x, y, w, h, texts: \[\], graphicKind/);
  assert.match(editorSource, /function PptxGraphicFramePreview\(/);
  assert.match(editorSource, /shape\.type === "graphic" && <PptxGraphicFramePreview/);
  assert.match(editorSource, /\{activeSlide\.shapes\.map\(\(shape\) => \{/);
  assert.match(editorSource, /const shapeStyle = pptxShapeVisualStyle\(shape\)/);
  assert.match(editorSource, /<PptxReadOnlySlide slide=\{slide\} thumbnail \/>/);
  assert.match(editorSource, /<PptxReadOnlySlide slide=\{slides\[presentingIdx\]\} \/>/);
  assert.doesNotMatch(editorSource, /serverSlideUrls/);
  assert.doesNotMatch(editorSource, /setSaveStatus\("saved"\);\s*void refreshPptxServerUrls\(request\.documentId\);/);
  assert.match(editorSource, /if \(!sourceUrl && docId\) \{\s*const renderedUrls = await refreshPptxServerUrls\(docId\)/);
  assert.match(editorSource, /fetchWithTimeout\(`\/api\/v1\/documents\/\$\{documentId\}\/slides`, \{ headers \}, 30_000\)/);
  assert.match(editorSource, /fetch\(input, \{ \.\.\.init, cache: "no-store", signal: controller\.signal \}\)/);
  assert.match(editorSource, /const pptxGraphicPreviewAbortRef = useRef<AbortController \| null>\(null\)/);
  assert.match(editorSource, /api\.documents\.presentationObjectBlob\([\s\S]*?controller\.signal/);
  assert.match(editorSource, /cursor < tasks\.length[\s\S]*?!controller\.signal\.aborted[\s\S]*?requestRevision === pptxGraphicPreviewRequestRef\.current/);
  assert.match(apiSource, /presentationObjectBlob:[\s\S]*?signal\?: AbortSignal[\s\S]*?\{ cache: false, signal \}/);
  assert.doesNotMatch(editorSource, /opacity: shape\.opacity,\s*borderRadius: pptxShapeBorderRadius/);
  assert.match(editorSource, /if \(!skipUndo\) pushUndo\(slidesRef\.current\);\s*slidesRef\.current = next;/);
  assert.match(editorSource, /const currentSlides = slidesRef\.current;\s*const sourceSlideIndex = currentSlides\.findIndex/);
  assert.match(editorSource, /copyShapeRequestRef\.current === requestId/);
  assert.match(presentationPatchSource, /presentationColorXml\(shape\.stroke\)\}/);
  assert.doesNotMatch(presentationPatchSource, /presentationColorXml\(shape\.stroke, shape\.opacity\)/);
});

test("Office loading fails closed instead of opening an empty editable canvas", () => {
  const docxLoader = editorSource.slice(
    editorSource.indexOf("// Load DOCX:"),
    editorSource.indexOf("// Load XLSX:"),
  );
  const pptxLoader = editorSource.slice(
    editorSource.indexOf("// Load PPTX:"),
    editorSource.indexOf("// Track line count for plain text and code modes"),
  );
  assert.match(editorSource, /const \[docxLoadError, setDocxLoadError\] = useState<string \| null>\(null\)/);
  assert.match(editorSource, /const \[xlsxLoadError, setXlsxLoadError\] = useState<string \| null>\(null\)/);
  assert.match(editorSource, /const \[pptxLoadError, setPptxLoadError\] = useState<string \| null>\(null\)/);
  assert.match(editorSource, /officeLoadError \? \([\s\S]*?<EmptyState[\s\S]*?office_file_load_error_description/);
  assert.match(editorSource, /\|\| Boolean\(officeLoadError\)/, "Save must be disabled after an Office parse failure");
  assert.doesNotMatch(editorSource, /setDocxHtml\(`<p>\$\{t\("page\.doc_editor\.failed_to_load_document_for_editing"\)\}<\/p>`\)/);
  assert.doesNotMatch(editorSource, /decodeOfficeTextFallback/, "binary Office files must never fall back to editable text");
  assert.match(editorSource, /if \(!zip\.file\("ppt\/presentation\.xml"\)\)/, "PPTX loading must validate its package identity");
  assert.doesNotMatch(docxLoader, /api\.documents\.getContent/, "DOCX parse failures must not fall back to extracted or binary text");
  assert.doesNotMatch(pptxLoader, /api\.documents\.getContent/, "PPTX parse failures must not rebuild from extracted or binary text");
});

test("Office-derived rich text is sanitized before entering the editor DOM", () => {
  assert.match(editorSource, /const rendered = await renderManorDocument\(buf\)/);
  assert.match(editorSource, /sanitizeManorDocumentRender\(rendered, sanitizeOptions\)/);
  assert.doesNotMatch(editorSource, /sanitizeDocumentHtml\(decodeOfficeTextFallback\(buf\)\)/);
  assert.match(editorSource, /innerHTML = sanitizeDocumentHtml\(docxHtml, \{\s*allowDocxEditorAttributes: true,\s*allowDocxLayoutStyles: true/);
  assert.match(sanitizerSource, /"data-docx-source-editable"/);
  assert.match(editorSource, /const sanitizedText = sanitizeDocumentHtml\(nextText, \{\s*allowDocxEditorAttributes: isDocx,\s*allowDocxLayoutStyles: isDocx/);
  assert.match(sanitizerSource, /ALLOW_DATA_ATTR: false/);
  assert.match(sanitizerSource, /data-docx-paragraph-index/);
});

test("rich-text paste and drop both sanitize transferred HTML", () => {
  assert.match(editorSource, /const insertRichTextTransfer = useCallback[\s\S]*?sanitizeDocumentHtml\(html\)/);
  assert.match(editorSource, /const handleRichTextPaste = useCallback[\s\S]*?insertRichTextTransfer\(event\.clipboardData\)\) focusRichEditorAfterChange\(\)/);
  assert.match(editorSource, /const handleRichTextDrop = useCallback[\s\S]*?event\.preventDefault\(\)[\s\S]*?insertRichTextTransfer\(event\.dataTransfer\)\) focusRichEditorAfterChange\(\)/);
  assert.match(editorSource, /const handleRichTextDragStart = useCallback[\s\S]*?richTextInternalDragRef\.current = canEditCurrentDoc/);
  assert.match(editorSource, /if \(richTextInternalDragRef\.current\) \{[\s\S]*?richTextInternalDragRef\.current = false;[\s\S]*?requestAnimationFrame\(\(\) => focusRichEditorAfterChange\(\)\)/);
  assert.match(editorSource, /onPaste=\{handleRichTextPaste\}[\s\S]*?onDragStart=\{handleRichTextDragStart\}[\s\S]*?onDragEnd=\{handleRichTextDragEnd\}[\s\S]*?onDrop=\{handleRichTextDrop\}/);
});

test("Office editor mode labels are translated", () => {
  assert.match(editorSource, /t\("page\.doc_editor\.mode_word_document"\)/);
  assert.match(editorSource, /t\("page\.doc_editor\.mode_presentation"\)/);
  assert.match(editorSource, /t\(`page\.doc_editor\.mode_\$\{mode\}`\)/);
  for (const localeName of ["en", "zh", "es", "fr"]) {
    assert.ok(localeNames.includes(localeName), `${localeName} must remain a registered locale`);
  }
  for (const localeSource of localeSources) {
    assert.match(localeSource, /"page\.doc_editor\.stretch_image":/);
    assert.match(localeSource, /"page\.doc_editor\.image_fit_failed":/);
    assert.match(localeSource, /"page\.doc_editor\.presentation_object_copy_failed":/);
    for (const key of editorModeTranslationKeys) {
      assert.match(localeSource, new RegExp(`"page\\.doc_editor\\.mode_${key}":`));
    }
  }
});

test("DOCX, XLSX, and PPTX retain their basic edit controls", () => {
  assert.match(editorSource, /contentEditable=\{canEditCurrentDoc && liveEditPreview\?\.mode !== "richtext"\}[\s\S]*?onInput=\{handleRichTextInput\}/);
  assert.match(editorSource, /value=\{activeCellValue\}[\s\S]*?onChange=\{\(event\) => updateCell\(selected\.r, selected\.c, event\.target\.value\)\}/);
  assert.match(editorSource, /onDoubleClick=\{\(e\) => \{[\s\S]*?beginTextEditing\(shape\.id, ti, t\.text\)/);
  assert.match(editorSource, /className="presentation-editor-edit-target"[\s\S]*?tabIndex=\{isEditingCell \? -1 : 0\}[\s\S]*?aria-keyshortcuts="Enter F2"/);
  assert.match(editorSource, /event\.key === "Enter" \|\| event\.key === "F2"[\s\S]*?beginTableCellEditing\(shape\.id, ri, ci, cell\)/);
  assert.match(editorSource, /tabIndex=\{isEditing \? -1 : 0\}[\s\S]*?role=\{isEditing \? undefined : "textbox"\}[\s\S]*?beginTextEditing\(shape\.id, ti, t\.text\)/);
  assert.match(editorSource, /event\.pointerType === "mouse" \|\| isEditing/);
  assert.match(indexCssSource, /\.presentation-editor-edit-target:focus-visible \{[\s\S]*?outline: 2px solid var\(--accent\)/);
  assert.match(editorSource, /aria-label=\{`\$\{t\("page\.doc_editor\.table"\)\} \$\{ri \+ 1\}, \$\{ci \+ 1\}`\}/);
  assert.match(editorSource, /event\.key === "Enter" && \(event\.metaKey \|\| event\.ctrlKey\)[\s\S]*?event\.currentTarget\.blur\(\)/);
  assert.match(editorSource, /role="status" aria-live="polite"[\s\S]*?t\("status\.loading"\)/);
  assert.match(
    editorSource,
    /isDocx \|\| isXlsx \|\| isPptx[\s\S]*?formatBytes\(doc\?\.file_size \|\| 0\)/,
    "binary Office editors should show the persisted file size instead of an empty text buffer",
  );
  assert.match(
    editorSource,
    /const displayValue = !isFormulaCell[\s\S]*?getSpreadsheetDisplayValue\(data, ri, ci, sourceDisplay, \{/,
    "formula cells should validate cached displays against the current sheet values",
  );
  assert.match(editorSource, /aria-label=\{t\("page\.doc_editor\.add_sheet"\)\}/);
  assert.match(editorSource, /onAddSheet=\{isXlsx \? handleAddXlsxSheet : undefined\}/);
  assert.match(editorSource, /onRenameSheet=\{isXlsx \? handleRenameXlsxSheet : undefined\}/);
  assert.match(editorSource, /onDoubleClick=\{\(\) => beginSheetRename\(sheet\)\}/);
  assert.match(editorSource, /event\.key === "F2" && onRenameSheet[\s\S]*?beginSheetRename\(sheet\)/);
  assert.match(editorSource, /aria-keyshortcuts=\{onRenameSheet \? "F2" : undefined\}/);
  assert.match(editorSource, /aria-label=\{t\("page\.doc_editor\.rename_sheet"\)\}/);
  assert.match(spreadsheetPatchSource, /renameSpreadsheetWorkbookSheets/);
  assert.match(spreadsheetPatchSource, /transformWorkbookSheetNameReferences/);
  assert.doesNotMatch(spreadsheetPatchSource, /Removing, renaming, or reordering worksheets/);
  assert.match(spreadsheetPatchSource, /appendSpreadsheetWorksheet/);
  for (const localeSource of localeSources) {
    assert.match(localeSource, /"page\.doc_editor\.new_sheet":/);
    assert.match(localeSource, /"page\.doc_editor\.add_sheet":/);
    assert.match(localeSource, /"page\.doc_editor\.rename_sheet":/);
    assert.match(localeSource, /"page\.doc_editor\.invalid_sheet_name":/);
  }
});

test("rejected AI presentation edits are not recorded as applied", () => {
  assert.match(editorLiveChatSource, /\) => boolean \| void \| Promise<boolean \| void>/);
  assert.match(
    editorSource,
    /if \(!nextSlides\) \{[\s\S]*?pptx_ai_invalid_edit[\s\S]*?return false;[\s\S]*?return previewPresentationLiveEdit\(nextSlides, targetId, meta\);/,
  );
  const streamedPatchApply = floatingChatSource.slice(
    floatingChatSource.indexOf("const applyPatchCommit"),
    floatingChatSource.indexOf("const applyPatchDelta"),
  );
  assert.match(streamedPatchApply, /const accepted = await detail\.adapter\.preview\(result\.content/);
  assert.match(streamedPatchApply, /if \(accepted === false\) throw new Error/);
  assert.ok(
    streamedPatchApply.indexOf("if (accepted === false)") < streamedPatchApply.indexOf("workingContent = result.content"),
    "streamed content must update only after the editor accepts it",
  );
  assert.match(floatingChatSource, /accepted === false[\s\S]*?editorLiveAppliedRef\.current = \{/);
  assert.match(
    floatingChatSource,
    /appliedPatchCount > 0[\s\S]*?hasCompletePatchProtocol \|\| hasGeneratedImageOnly[\s\S]*?await finishLiveEdit\(/,
    "only a complete patch protocol or generated-image edit may become reviewable",
  );
  assert.match(
    floatingChatSource,
    /invalidPatchProtocol[\s\S]*?await rollbackFailedLiveEdit\(\)/,
    "an incomplete streamed patch must roll back its temporary preview",
  );
  assert.match(
    floatingChatSource,
    /patchFailureCount > 0\s*\?\s*"assistant patch \(partial\)"/,
  );
});

test("AI Edit reuses the Floating Chat shell and keeps review controls out of the canvas", () => {
  const floatingPanelStart = floatingChatSource.indexOf("<FloatingPanel");
  const floatingPanelEnd = floatingChatSource.indexOf(">", floatingPanelStart);
  const floatingPanelUsage = floatingChatSource.slice(
    floatingPanelStart,
    floatingPanelEnd + 1,
  );
  assert.ok(floatingPanelStart >= 0 && floatingPanelEnd > floatingPanelStart);
  assert.match(floatingPanelUsage, /open=\{open\}/);
  assert.doesNotMatch(floatingPanelUsage, /className=|style=|width=|height=/);
  assert.match(floatingPanelSource, /className="floating-panel"/);
  assert.doesNotMatch(
    floatingPanelSource,
    /width\?:|height\?:|style\?:|className\?:/,
  );
  assert.doesNotMatch(floatingChatSource, /aiEditDocked|floating-panel--ai-edit/);
  assert.doesNotMatch(
    indexCssSource,
    /ai-edit-docked|floating-panel--ai-edit|--ai-edit-dock-/,
  );
  const statusbarStart = editorSource.indexOf("<div className={`manor-editor-statusbar${liveEditPreview");
  const statusbarEnd = editorSource.indexOf("<MediaInsertDialog", statusbarStart);
  assert.ok(statusbarStart >= 0 && statusbarEnd > statusbarStart);
  assert.match(editorSource.slice(statusbarStart, statusbarEnd), /<AiEditPreviewControls/);
  const previewRule = indexCssSource.match(/\.doc-editor-live-preview-bar \{([\s\S]*?)\}/)?.[1] || "";
  assert.doesNotMatch(previewRule, /position:\s*absolute|bottom:|transform:/);
  assert.match(previewRule, /flex:\s*1 1 420px/);
});

test("AI Edit starter suggestions are real composer actions", () => {
  const suggestionsStart = floatingChatSource.indexOf("editorLiveExamples.map");
  const suggestionsEnd = floatingChatSource.indexOf(
    "!showConversationSkeleton && visibleWorkflowMessageEntries.map",
    suggestionsStart,
  );
  const suggestionSource = floatingChatSource.slice(suggestionsStart, suggestionsEnd);
  assert.ok(suggestionsStart >= 0 && suggestionsEnd > suggestionsStart);
  assert.match(suggestionSource, /<button/);
  assert.match(suggestionSource, /type="button"/);
  assert.match(suggestionSource, /className="floating-chat-suggestion"/);
  assert.match(suggestionSource, /setInput\(example\)/);
  assert.match(suggestionSource, /composerEditorRef\.current\?\.focus\(\)/);
  assert.doesNotMatch(suggestionSource, /<span\s+key=\{example\}/);
  assert.match(
    indexCssSource,
    /\.floating-chat-suggestion:focus-visible \{[\s\S]*?var\(--accent-ring/,
  );
});

test("Office files remain editable without a format protection mode", () => {
  const spreadsheetEditor = editorSource.slice(
    editorSource.indexOf("function SpreadsheetEditor"),
    editorSource.indexOf("// Presentation Editor sub-component"),
  );
  assert.doesNotMatch(editorSource, /fidelityMode|docxFidelityMode|xlsxFidelityMode|pptxFidelityMode/);
  assert.match(spreadsheetEditor, /<SheetToolbarButton onClick=\{addRow\}/);
  assert.doesNotMatch(spreadsheetEditor, /pointerEvents:\s*[^\n]*fidelity/);
  assert.doesNotMatch(editorSource, /shape\.source && !shape\.source\.editable/);
  assert.doesNotMatch(editorSource, /DocumentPreservationError[\s\S]*?buildDocumentFile/);
  assert.match(editorSource, /editDocumentFileWithSnapshot\([\s\S]*?docxOriginalBuffer,[\s\S]*?docxBaselineHtml,[\s\S]*?text,[\s\S]*?documentName,/);
  assert.doesNotMatch(editorSource, /PresentationPreservationError[\s\S]*?buildPresentationFile/);
  assert.doesNotMatch(editorSource, /buildPresentationFile/);
  assert.doesNotMatch(editorSource, /textToSlides/);
  assert.match(editorSource, /return \{ \.\.\.s, bg: color, bgGrad: undefined, bgImgUrl: undefined \}/);
  assert.match(editorSource, /The original Word package is unavailable; reload the document before saving\./);
  assert.match(editorSource, /The original PowerPoint package is unavailable; reload the presentation before saving\./);
  assert.match(
    editorSource,
    /mergePresentationSavedIdentity\([\s\S]*?pptxSlidesRef\.current,[\s\S]*?savedSlides,[\s\S]*?request\.slides,[\s\S]*?\);[\s\S]*?pptxSlidesRef\.current = nextSlides;[\s\S]*?setPptxSlides\(nextSlides\)/,
    "successful PPTX saves should synchronize persisted source identities into the live editor state",
  );
  assert.match(
    editorSource,
    /const \{ sourceMap: _savedEditProvenance, \.\.\.savedParagraphState \} = paragraph;/,
    "successful PPTX saves should clear edit provenance that was relative to the previous package",
  );
  assert.doesNotMatch(editorSource, /SpreadsheetPreservationError[\s\S]*?buildSpreadsheetFile/);
  assert.doesNotMatch(spreadsheetPatchSource, /if \(!structurallyCompatible\) \{[\s\S]*?return buildSpreadsheetFile/);
  assert.match(spreadsheetPatchSource, /transformWorksheetStructureXml/);
  assert.match(spreadsheetPatchSource, /createSpreadsheetStyleRegistry/);
  assert.match(presentationPatchSource, /shapeObjectXml/);
  assert.match(editorSource, /sourceDataRef\.current = structuredClone\(normalized\);[\s\S]*?setData\(normalized\)/);
  assert.doesNotMatch(viewerSource, /legacyOfficeReadOnly/);
  assert.doesNotMatch(
    editorSource,
    /if \(!doc \|\| !isLegacyOfficeFile\(doc\.name\)\) return;\s*navigate\(`\/viewer/,
  );
  assert.match(editorSource, /OfficeEditorFileFactory\.create\(/);
  assert.match(editorSource, /const needsLegacyOfficeConversion = editorFile\.requiresLegacyConversion/);
  assert.match(apiSource, /editableResponse:[\s\S]*?"editable-file"/);
  assert.match(editorSource, /const source = await loadOfficeEditSource\(docId, needsLegacyOfficeConversion\)/);
});

test("Office editor controls remain usable and understandable on narrow screens", () => {
  assert.match(editorSource, /className="spreadsheet-editor-workspace"/);
  assert.match(editorSource, /className="spreadsheet-editor-grid-pane"/);
  assert.match(editorSource, /<SpreadsheetImageLayer\s+images=\{nativeImages\}/);
  assert.match(editorSource, /className="spreadsheet-editor-chart-pane spreadsheet-editor-chart-pane--native"/);
  assert.match(editorSource, /aria-label=\{title\}[\s\S]*?aria-pressed=\{active \|\| undefined\}/);
  assert.match(editorSource, /className="presentation-editor-slide-strip" role="tablist"/);
  assert.match(editorSource, /role="tab"[\s\S]*?aria-label=\{`\$\{t\("page\.file_viewer\.slide"\)\} \$\{idx \+ 1\}`\}[\s\S]*?onKeyDown=\{\(event\) => handlePresentationThumbnailKeyDown\(event, idx\)\}/);
  for (const localeSource of localeSources) {
    assert.doesNotMatch(localeSource, /"page\.doc_editor\.rows":\s*"[^"]*·/);
  }
});

test("PPTX transparent fills do not hide their text or neighboring shape content", () => {
  assert.match(editorSource, /shape\.fill = fillColor \? presentationColorWithAlpha\(fillColor, solidFill\) : undefined/);
  assert.doesNotMatch(editorSource, /const alphaM = sp\.match\([\s\S]*?shape\.opacity = parseInt\(alphaM\[1\]/);
});
