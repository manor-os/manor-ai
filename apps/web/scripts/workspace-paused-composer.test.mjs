#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const workspaceChatSource = await readFile(
  new URL("../src/components/WorkspaceChat.tsx", import.meta.url),
  "utf8",
);
const chatInputFooterSource = await readFile(
  new URL("../src/components/ChatInputFooter.tsx", import.meta.url),
  "utf8",
);

test("paused workspaces keep direct chat available while guarded actions stay paused", () => {
  assert.match(
    workspaceChatSource,
    /const composerDisabled = showSimulationRuntime;/,
  );
  assert.doesNotMatch(
    workspaceChatSource,
    /const composerDisabled = workspace\?\.status === "paused"/,
  );
  assert.match(
    workspaceChatSource,
    /<ChatInputFooter[\s\S]*?disabled=\{composerDisabled \|\| taskSession\?\.hostAvailable === false\}/,
  );
  assert.match(
    workspaceChatSource,
    /<ChatModeTemplateGallery[\s\S]*?disabled=\{streaming \|\| composerDisabled\}/,
  );
  assert.match(
    workspaceChatSource,
    /<ChatModeToolbar[\s\S]*?disabled=\{streaming \|\| composerDisabled\}/,
  );
  assert.match(
    workspaceChatSource,
    /workspacePaused=\{workspace\?\.status === "paused"\}/,
  );
  assert.match(chatInputFooterSource, /contentEditable=\{!streaming && !disabled\}/);
  assert.match(chatInputFooterSource, /aria-disabled=\{disabled \|\| streaming\}/);
  assert.match(chatInputFooterSource, /disabled=\{disabled \|\| streaming \|\| voice\.busy \|\| !canSend\}/);
});
