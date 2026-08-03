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
  assert.match(modalSource, /FOCUSABLE_SELECTOR/);
  assert.match(modalSource, /button:not\(\[disabled\]\)/);
  assert.match(modalSource, /input:not\(\[disabled\]\):not\(\[type="hidden"\]\)/);
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
