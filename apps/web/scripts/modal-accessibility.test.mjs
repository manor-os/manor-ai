import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const modalSource = await readFile(
  new URL("../src/components/ui/Modal.tsx", import.meta.url),
  "utf8",
);
const confirmDialogSource = await readFile(
  new URL("../src/components/ui/ConfirmDialog.tsx", import.meta.url),
  "utf8",
);
const floatingPanelSource = await readFile(
  new URL("../src/components/FloatingPanel.tsx", import.meta.url),
  "utf8",
);
const floatingChatSource = await readFile(
  new URL("../src/components/FloatingChat.tsx", import.meta.url),
  "utf8",
);
const supportPanelSource = await readFile(
  new URL("../src/components/SupportPanel.tsx", import.meta.url),
  "utf8",
);
const appLayoutSource = await readFile(
  new URL("../src/layouts/AppLayout.tsx", import.meta.url),
  "utf8",
);
const cssSource = await readFile(new URL("../src/index.css", import.meta.url), "utf8");

test("shared modal moves focus inside and restores the previous element", () => {
  assert.match(modalSource, /useRef/);
  assert.match(modalSource, /dialogRef/);
  assert.match(modalSource, /previouslyFocusedElementRef/);
  assert.match(modalSource, /activeElement instanceof HTMLElement/);
  assert.match(modalSource, /focusableElements\[0\] \?\? dialog/);
  assert.match(modalSource, /previouslyFocusedElement\?\.isConnected/);
  assert.match(modalSource, /previouslyFocusedElement\.focus\(\)/);
  assert.match(modalSource, /ref=\{dialogRef\}/);
  assert.match(modalSource, /tabIndex=\{-1\}/);
});

test("shared modal falls back only when its original focus target is gone", () => {
  assert.match(modalSource, /restoreFocusFallback\?: \(\) => void/);
  assert.match(modalSource, /restoreFocusFallbackRef/);
  const restoreStart = modalSource.indexOf(
    "const previouslyFocusedElement = previouslyFocusedElementRef.current",
  );
  const restoreEnd = modalSource.indexOf("\n    };", restoreStart);
  const restoreSource = modalSource.slice(restoreStart, restoreEnd);
  assert.ok(
    restoreSource.indexOf("previouslyFocusedElement?.isConnected")
      < restoreSource.indexOf("previouslyFocusedElement.focus()"),
  );
  assert.ok(
    restoreSource.indexOf("previouslyFocusedElement.focus()")
      < restoreSource.indexOf("restoreFocusFallbackRef.current?.()"),
  );
  assert.match(confirmDialogSource, /restoreFocusFallback\?: \(\) => void/);
  assert.match(confirmDialogSource, /restoreFocusFallback=\{restoreFocusFallback\}/);
});

test("shared modal traps forward and reverse tab navigation", () => {
  assert.match(modalSource, /export function trapDialogTabKey/);
  assert.match(modalSource, /FOCUSABLE_SELECTOR/);
  assert.match(modalSource, /button:not\(\[disabled\]\)/);
  assert.match(modalSource, /input:not\(\[disabled\]\):not\(\[type="hidden"\]\)/);
  assert.match(modalSource, /audio\[controls\]:not\(\[tabindex="-1"\]\)/);
  assert.match(modalSource, /video\[controls\]:not\(\[tabindex="-1"\]\)/);
  assert.match(modalSource, /iframe:not\(\[tabindex="-1"\]\)/);
  assert.match(modalSource, /getComputedStyle/);
  assert.match(modalSource, /visibility !== "hidden"/);
  assert.match(modalSource, /getClientRects\(\)\.length > 0/);
  assert.match(modalSource, /event\.key !== "Tab"/);
  assert.match(modalSource, /event\.shiftKey/);
  assert.match(modalSource, /event\.preventDefault\(\)/);
  assert.match(modalSource, /first\.focus\(\)/);
  assert.match(modalSource, /last\.focus\(\)/);
  assert.match(modalSource, /event\.key === "Escape"/);
  assert.match(modalSource, /onClose\(\)/);
});

test("shared modal limits keyboard handling and scroll locking to the active layer", () => {
  assert.match(modalSource, /const MODAL_LAYER_ATTRIBUTE/);
  assert.match(modalSource, /document\.body\.querySelectorAll<HTMLElement>/);
  assert.match(modalSource, /renderedLayers\.item\(renderedLayers\.length - 1\)/);
  assert.match(modalSource, /data-manor-modal-layer-id=\{modalLayerId\}/);
  assert.match(modalSource, /function isTopModalLayer/);
  assert.match(modalSource, /if \(!isTopModalLayer\(modalLayerId\)\) return/);
  assert.match(modalSource, /let bodyScrollLockCount = 0/);
  assert.match(modalSource, /function lockBodyScroll/);
  assert.match(modalSource, /bodyOverflowBeforeLock = document\.body\.style\.overflow/);
  assert.match(modalSource, /document\.body\.style\.overflow = bodyOverflowBeforeLock/);
});

test("shared modal contains long titles without shrinking the close action", () => {
  assert.match(modalSource, /className="manor-dialog-title" title=\{title\}/);
  assert.match(
    cssSource,
    /\.manor-dialog-title\s*\{[^}]*min-width:\s*0[^}]*overflow:\s*hidden[^}]*text-overflow:\s*ellipsis[^}]*white-space:\s*nowrap/,
  );
  assert.match(
    cssSource,
    /\.manor-dialog-close\s*\{[^}]*flex:\s*0 0 auto/,
  );
});

test("shared confirmation dialog exposes compact accessible errors", () => {
  assert.match(confirmDialogSource, /error\?: string/);
  assert.match(confirmDialogSource, /error &&/);
  assert.match(confirmDialogSource, /className="confirm-dialog-error"/);
  assert.match(confirmDialogSource, /role="alert"/);
  assert.doesNotMatch(confirmDialogSource, /#57534e/);
  assert.match(
    cssSource,
    /\.confirm-dialog-error\s*\{[^}]*var\(--editor-danger-bg\)[^}]*var\(--editor-danger-text\)/,
  );
});

test("closed floating panels are inert and restore focus after closing", () => {
  assert.match(floatingPanelSource, /!open \? \{ inert: "" \} : \{\}/);
  assert.match(floatingPanelSource, /aria-hidden=\{!open\}/);
  assert.match(floatingPanelSource, /data-open=\{open \? "true" : "false"\}/);
  assert.match(floatingPanelSource, /previouslyFocusedElementRef/);
  assert.match(floatingPanelSource, /initialFocusRef\?\.current/);
  assert.match(floatingPanelSource, /restoreFocusRef\?\.current/);
  assert.match(floatingPanelSource, /restoreTarget\?\.isConnected/);
  assert.match(floatingPanelSource, /ref=\{panelRef\}/);
  assert.match(floatingPanelSource, /tabIndex=\{-1\}/);
});

test("only the top floating panel handles Escape through its close lifecycle", () => {
  assert.match(floatingPanelSource, /topFloatingPanelEscapeEntry/);
  assert.match(floatingPanelSource, /entry\.zIndex >= top\.zIndex/);
  assert.match(floatingPanelSource, /event\.key !== "Escape"/);
  assert.match(floatingPanelSource, /topFloatingPanelEscapeEntry\(\) !== entry/);
  assert.match(floatingPanelSource, /data-manor-modal-layer-id/);
  assert.match(floatingPanelSource, /document\.addEventListener\("keydown", handleKeyDown, true\)/);
  assert.match(floatingPanelSource, /escapeCloseRef\.current/);
  assert.match(floatingPanelSource, /\.catch\(\(error\) =>/);
  assert.match(floatingPanelSource, /void entry\.close\(\)/);
  assert.doesNotMatch(supportPanelSource, /window\.addEventListener\("keydown"/);
  assert.match(supportPanelSource, /onClose=\{onClose\}/);
  assert.match(floatingChatSource, /onClose=\{handleCloseChat\}/);
});

test("floating chat exposes its dialog relationship and restores the actual opener", () => {
  assert.match(floatingChatSource, /aria-label=\{t\("component\.floating_chat\.chat_with_manor_ai"\)\}/);
  assert.match(floatingChatSource, /aria-expanded=\{open\}/);
  assert.match(floatingChatSource, /aria-controls="floating-chat-panel"/);
  assert.match(floatingChatSource, /aria-hidden=\{open\}/);
  assert.match(floatingChatSource, /tabIndex=\{open \? -1 : 0\}/);
  assert.match(floatingChatSource, /id="floating-chat-panel"/);
  assert.match(floatingChatSource, /initialFocusRef=\{composerEditorRef\}/);
  assert.doesNotMatch(floatingChatSource, /floatingChatButtonRef/);
  assert.doesNotMatch(floatingChatSource, /restoreFocusRef=/);
  assert.match(floatingChatSource, /editorRef=\{composerEditorRef\}/);
  assert.doesNotMatch(
    floatingChatSource,
    /setTimeout\(\(\) => composerEditorRef\.current\?\.focus\(\), 200\)/,
  );
});

test("support panel restores focus to the stable More launcher", () => {
  assert.match(supportPanelSource, /id="floating-support-panel"/);
  assert.match(supportPanelSource, /restoreFocusRef=\{restoreFocusRef\}/);
  assert.match(appLayoutSource, /const supportRestoreFocusRef = useRef<HTMLButtonElement>\(null\)/);
  assert.match(appLayoutSource, /ref=\{supportRestoreFocusRef\}/);
  assert.match(appLayoutSource, /aria-controls="floating-support-panel"/);
  assert.match(appLayoutSource, /restoreFocusRef=\{supportRestoreFocusRef\}/);
});
