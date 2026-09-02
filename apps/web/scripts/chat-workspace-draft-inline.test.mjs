import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const chatSource = fs.readFileSync(
  new URL("../src/components/EmbeddedChat.tsx", import.meta.url),
  "utf8",
);
const draftPageSource = fs.readFileSync(
  new URL("../src/pages/WorkspaceDraftChat.tsx", import.meta.url),
  "utf8",
);
const panelSource = fs.readFileSync(
  new URL("../src/components/WorkspaceDraftConfigurationPanel.tsx", import.meta.url),
  "utf8",
);
const cssSource = fs.readFileSync(
  new URL("../src/index.css", import.meta.url),
  "utf8",
);
const apiSource = fs.readFileSync(
  new URL("../src/lib/api.ts", import.meta.url),
  "utf8",
);
const liveE2eSource = fs.readFileSync(
  new URL("../e2e/chat-workspace-draft-live.spec.ts", import.meta.url),
  "utf8",
);
const chatStreamSource = fs.readFileSync(
  new URL("../src/lib/chatStream.ts", import.meta.url),
  "utf8",
);

test("creation preview exposes measurement definitions and manual collection honestly", () => {
  for (const field of ["definition", "source", "unit", "window", "collection", "missing", "manual", "automatic"]) {
    assert.ok(panelSource.includes(`page.workspace_draft_chat.measurement.${field}`));
  }
  assert.match(panelSource, /item\.measurement \|\| asList\(fields\.stats\)/);
  assert.match(panelSource, /api\.workspaces\.stats\.library\(\)/);
});

test("Draft channel selection uses server options and blocks creation while saving", () => {
  assert.match(panelSource, /_blueprint_channel_requirements/);
  assert.match(panelSource, /blueprint_channel_config_ids:/);
  assert.match(panelSource, /requirement\.resource_options\.map/);
  assert.match(panelSource, /ariaLabel=\{requirement\.label\}/);
  assert.match(panelSource, /const busy = .*channelMutation\.isPending/);
  assert.match(panelSource, /onChange=\{\(id\) => channelMutation\.mutate/);
});

test("ordinary Chat projects Workspace drafts without a navigation CTA", () => {
  assert.match(chatSource, /continue_workspace_draft/);
  assert.match(chatSource, /artifact_kind === "workspace_draft"/);
  assert.match(
    chatSource,
    /artifact\.kind === "workspace"[\s\S]*?<WorkspaceDraftConfigurationPanel/,
  );
  assert.doesNotMatch(
    chatSource,
    /kind:\s*"workspace"[\s\S]{0,180}href:\s*parsed\.deep_link/,
  );
  assert.match(
    chatSource,
    /const activeOutputArtifact = useMemo\([\s\S]*?artifactDedupKey\(artifact\) === selectedKey/,
  );
  assert.match(chatSource, /keepLatestWorkspaceDraftArtifacts/);
  assert.match(chatSource, /latestMessageByDraft\.set\(artifactDedupKey\(artifact\), messageIndex\)/);
  assert.match(chatSource, /artifact=\{activeOutputArtifact\}/);
  assert.match(chatStreamSource, /rawResult:\s*workspaceDraftPublicResult\(tc\)/);
  assert.match(chatStreamSource, /parsed\.artifact_kind !== "workspace_draft"/);
  assert.match(chatStreamSource, /WORKSPACE_DRAFT_PUBLIC_KEYS/);
  assert.match(chatSource, /parseArtifactToolResult\(tool\)/);
  assert.match(chatSource, /tool\.rawResult \?\? tool\.result/);
});


test("blank Workspace creation starts assistant discovery automatically", () => {
  assert.doesNotMatch(
    draftPageSource,
    /if\s*\(\s*!draftId\s*&&\s*\(\s*initialBriefParam\s*\|\|\s*resumingOpening\s*\)\s*&&\s*!startedRef\.current\s*&&\s*!createMutation\.isPending\s*\)/,
  );
  assert.match(
    draftPageSource,
    /if \(!draftId && !startedRef\.current && !createMutation\.isPending\)[\s\S]*?createMutation\.mutate\(initialBriefParam \|\| undefined\)/,
  );
  assert.match(
    draftPageSource,
    /if \(!draftId \|\| !draft\) \{[\s\S]*?createMutation\.mutate\(v\);[\s\S]*?return;/,
  );
  assert.match(draftPageSource, /disabled=\{Boolean\(draftId\) && \(!draft \|\| !editable\)\}/);
});

test("draft message transport failure restores input and reconciles a completed turn", () => {
  assert.match(draftPageSource, /onError: \(err: Error, \{ id, message \}\) =>/);
  assert.match(
    draftPageSource,
    /setInput\(\(current\) => current\.trim\(\) \? current : message\)/,
  );
  assert.match(
    draftPageSource,
    /api\.workspaceDrafts\.get\(id\)\.then\(\(freshDraft\) => \{[\s\S]*?setDraft\(freshDraft\)/,
  );
  assert.match(
    draftPageSource,
    /_hasCompletedDraftTurn\(freshDraft\.messages, message\)[\s\S]*?setMessages\(freshDraft\.messages\.map/,
  );
  assert.doesNotMatch(
    draftPageSource,
    /onError: \(err: Error\) => \{[\s\S]{0,180}setMessages\(\(prev\) => prev\.slice\(0, -2\)\)/,
  );
});

test("draft projection uses one borderless information surface", () => {
  const workspaceDraftCss = cssSource.slice(
    cssSource.indexOf("/* Workspace draft projection shared by ordinary Chat and creation Chat. */"),
    cssSource.indexOf('html[data-theme="dark"] .chat-artifact-summary-download'),
  );
  assert.match(cssSource, /\.workspace-draft-panel\s*\{[\s\S]*?background:\s*var\(--surface-panel\)/);
  assert.match(
    cssSource,
    /\.chat-output-panel > \.workspace-draft-panel > \.workspace-draft-panel__body\s*\{[\s\S]*?overflow-y:\s*auto/,
  );
  assert.match(
    cssSource,
    /\.chat-output-panel > \.workspace-draft-panel\s*\{[\s\S]*?overflow:\s*hidden/,
  );
  assert.match(
    cssSource,
    /@container chat-workbench \(max-width: 720px\)\s*\{[\s\S]*?\.embedded-chat-output-panes\.resizable-pane-group\s*\{[\s\S]*?display:\s*grid/,
  );
  assert.match(
    cssSource,
    /\.chat-output-panel:has\(> \.workspace-draft-panel\)\s*\{[\s\S]*?max-height:\s*min\(68vh, 680px\)/,
  );
  assert.match(chatSource, /minSize:\s*outputOpen \? 360 : undefined/);
  assert.match(panelSource, /workspace-draft-panel__body[\s\S]*?workspace-draft-panel__footer/);
  assert.match(cssSource, /\.workspace-draft-panel__footer > button\s*\{[\s\S]*?width:\s*100%/);
  assert.doesNotMatch(
    workspaceDraftCss,
    /\.workspace-draft-panel__footer\s*\{[\s\S]*?position:\s*sticky/,
  );
  assert.doesNotMatch(draftPageSource, /\{\/\* Status \+ summary \*\/\}/);
  assert.match(cssSource, /conic-gradient\(var\(--accent\)/);
  assert.doesNotMatch(
    workspaceDraftCss,
    /\bborder(?:-top|-right|-bottom|-left)?:\s/,
  );
});

test("live coverage enters through the ordinary Chat composer", () => {
  assert.match(liveE2eSource, /composer\.fill\(startPrompt\)/);
  assert.match(liveE2eSource, /composer\.fill\(continuePrompt\)/);
  assert.match(liveE2eSource, /ask me one concise question to confirm the Goal/);
  assert.match(liveE2eSource, /expect\.soft\(initialDraft\.fields\._creation_preferences\)\.toEqual\(\{\s*goal_confirmed: false, autonomy_confirmed: false/);
  assert.match(liveE2eSource, /an unconfirmed draft must not be ready/);
  assert.match(liveE2eSource, /preferences\.autonomy_confirmed === true/);
  assert.match(liveE2eSource, /Configure a Goal titled/);
  assert.match(liveE2eSource, /Run automatically after creation/);
  assert.match(liveE2eSource, /await runtimeMode\.click\(\)/);
  assert.match(liveE2eSource, /\/api\/v1\/chat\/conversations\/\$\{conversationId\}\/messages/);
  assert.match(liveE2eSource, /persisted\.messages/);
  assert.doesNotMatch(liveE2eSource, /runtime_start_workspace_draft_action/);
  assert.doesNotMatch(liveE2eSource, /runtime_continue_workspace_draft_action/);
  assert.doesNotMatch(liveE2eSource, /db\.add_all/);
});
