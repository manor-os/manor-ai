#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const source = await readFile(
  new URL("../src/components/ChatInputFooter.tsx", import.meta.url),
  "utf8",
);
const floatingSource = await readFile(
  new URL("../src/components/FloatingChat.tsx", import.meta.url),
  "utf8",
);

test("choosing a local file dismisses every attachment picker", () => {
  assert.match(
    source,
    /const closeAttachmentPickers = useCallback\([\s\S]*?setAttachMenuOpen\(false\);[\s\S]*?setKbPickerOpen\(false\);[\s\S]*?setKbSearch\(""\);/,
  );
  assert.match(
    source,
    /const handleFileSelect = useCallback\([\s\S]*?closeAttachmentPickers\(\);/,
  );
});

test("choosing an already attached Knowledge document still dismisses its picker", () => {
  assert.match(
    source,
    /const addKbDoc = \(doc: ComposerDocumentOption\) => \{[\s\S]*?closeAttachmentPickers\(\);[\s\S]*?if \(attachedFiles\.some\(\(f\) => f\.id === doc\.id\)\) return;/,
  );
});

test("Knowledge attachments selected with + are sent directly from attachments", () => {
  assert.match(
    source,
    /const addKbDoc = \(doc: ComposerDocumentOption\) => \{[\s\S]*?closeAttachmentPickers\(\);[\s\S]*?dismissHashAutocomplete\(true, false\);[\s\S]*?if \(attachedFiles\.some\(\(f\) => f\.id === doc\.id\)\) return;[\s\S]*?setAttachedFiles\(\(prev\) => \[\.\.\.prev, composerPreviewItemFromDoc\(doc\)\]\);/,
  );
  assert.match(
    source,
    /function composerPreviewItemFromDoc\(doc: ComposerDocumentOption\): AttachedItem \{[\s\S]*?fsPath: doc\.fs_path \|\| undefined,/s,
  );
  assert.doesNotMatch(
    source,
    /const addKbDoc = \(doc: ComposerDocumentOption\) => \{[\s\S]*?hasInlineToken\(value, token\)/,
  );
  assert.doesNotMatch(
    source,
    /const snapshot = currentAttachments\.filter\([\s\S]*?item\.type !== "knowledge"[\s\S]*?hasInlineToken\(value, `#\$\{item\.name\}`\)/,
  );
  assert.match(
    source,
    /const snapshot = currentAttachments;/,
  );
  assert.match(
    source,
    /attachedFiles\.length > 0 && \([\s\S]*?attachedFiles\.map\(\(f, i\) =>/,
  );
  assert.doesNotMatch(
    source,
    /f\.type === "knowledge" \? null :/,
  );
});

test("FloatingChat knowledge attachments keep the resolved fs_path in snapshots", () => {
  assert.match(
    floatingSource,
    /const addKbDoc = \(doc: \{ id: string; name: string; fs_path\?: string \| null \}\) => \{[\s\S]*?fsPath: doc\.fs_path \|\| undefined,[\s\S]*?type: "knowledge"/s,
  );
  assert.match(
    floatingSource,
    /const selectHashDoc = useCallback\([\s\S]*?fsPath: doc\.fs_path \|\| undefined,[\s\S]*?type: "knowledge"/s,
  );
});

test("hash file autocomplete removes the typed trigger instead of re-inserting a token", () => {
  assert.match(
    source,
    /const selectHashDoc = useCallback\([\s\S]*?dismissHashAutocomplete\(true\);/s,
  );
  assert.doesNotMatch(
    source,
    /const selectHashDoc = useCallback\([\s\S]*?const token = `#\$\{doc\.name\}`;/s,
  );
});

test("hash file autocomplete can be dismissed after accidental opening", () => {
  assert.match(
    source,
    /const dismissHashAutocomplete = useCallback\(/,
  );
  assert.match(
    source,
    /if \(hashDropdownOpen\) dismissHashAutocomplete\(true, false\);/,
  );
  assert.match(
    source,
    /const triggerSend = useCallback\([\s\S]*?dismissHashAutocomplete\(false, false\);[\s\S]*?onSend/,
  );
  assert.match(
    source,
    /chat-composer-hash-title[\s\S]*?chat-composer-autocomplete-close[\s\S]*?onClick=\{\(\) => dismissHashAutocomplete\(true\)\}/,
  );
});

test("hash file autocomplete handles Escape even when there are no matches", () => {
  assert.match(
    source,
    /if \(hashDropdownOpen\) \{[\s\S]*?if \(e\.key === "ArrowDown" && hashFiltered\.length > 0\)[\s\S]*?if \(e\.key === "Escape"\) \{[\s\S]*?dismissHashAutocomplete\(true\);/,
  );
});
