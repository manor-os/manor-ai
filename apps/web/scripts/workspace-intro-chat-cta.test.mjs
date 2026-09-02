import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = (path) => readFile(new URL(path, import.meta.url), "utf8");

test("ordinary Manor Chat exposes the Workspace intro without adding it to DMs", async () => {
  const [layout, embeddedChat] = await Promise.all([
    source("../src/layouts/AppLayout.tsx"),
    source("../src/components/EmbeddedChat.tsx"),
  ]);

  assert.match(layout, /showWorkspaceIntro=\{activeConvType === "manor"\}/);
  assert.match(embeddedChat, /showWorkspaceIntro\?: boolean/);
  assert.match(embeddedChat, /\{showWorkspaceIntro && <WorkspaceIntroDialog \/>\}/);
});

test("Workspace intro stays minimal and keeps creation on the existing route", async () => {
  const [dialog, workspaces] = await Promise.all([
    source("../src/components/workspaces/WorkspaceIntroDialog.tsx"),
    source("../src/pages/Workspaces.tsx"),
  ]);

  assert.match(dialog, /<Modal[\s\S]*?component\.workspace_intro\.dialog_title/);
  assert.match(dialog, /maxWidth="560px"/);
  assert.match(dialog, /<video[\s\S]*?controls[\s\S]*?<source src=\{WORKSPACE_EXPLAINER_VIDEO_URL\}/);
  assert.match(dialog, /component\.workspace_intro\.headline/);
  assert.match(dialog, /component\.workspace_intro\.summary/);
  assert.doesNotMatch(dialog, /component\.workspace_intro\.how_/);
  assert.match(dialog, /aria-describedby=\{explanationId\}/);
  assert.doesNotMatch(dialog, /EXPLANATION_POINTS/);
  assert.doesNotMatch(dialog, /component\.workspace_intro\.not_now/);
  assert.doesNotMatch(dialog, /component\.workspace_intro\.open/);
  assert.match(dialog, /const WORKSPACE_EXPLAINER_VIDEO_URL/);
  assert.doesNotMatch(dialog, /workspaceIntroMotionUrl/);
  assert.doesNotMatch(workspaces, /workspaceIntroMotionUrl/);
  assert.match(workspaces, /const WORKSPACE_INTRO_MOTION_URL/);
  assert.match(workspaces, /const WORKSPACE_INTRO_DARK_MOTION_URL/);
  assert.match(workspaces, /new MutationObserver\(syncMotionUrl\)/);
  assert.match(dialog, /navigate\("\/workspaces\/new"\)/);
});

test("Workspace intro motion respects reduced-motion preferences", async () => {
  const [dialog, sharedStyles] = await Promise.all([
    source("../src/components/workspaces/WorkspaceIntroDialog.tsx"),
    source("../src/index.css"),
  ]);

  assert.match(dialog, /import AiEditButton from "\.\.\/ui\/AiEditButton"/);
  assert.match(dialog, /<AiEditButton[\s\S]*?workspace-intro-launcher/);
  assert.match(dialog, /autoPlay=\{!reduceMotion\}/);
  assert.match(dialog, /loop=\{!reduceMotion\}/);
  assert.match(dialog, /ref=\{videoRef\}/);
  assert.match(dialog, /if \(shouldReduceMotion\) videoRef\.current\?\.pause\(\)/);
  assert.match(dialog, /motionPreference\.addEventListener\("change", syncReducedMotion\)/);
  assert.match(dialog, /\}, \[\]\);/);
  assert.doesNotMatch(dialog, /if \(!open/);
  assert.match(sharedStyles, /@media \(prefers-reduced-motion: reduce\)[\s\S]*?\.ai-edit-button/);
  assert.match(sharedStyles, /\.ai-edit-button__[\s\S]*?animation: none !important/);
});
