#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const workspaceChat = readFileSync(
  new URL("../src/components/WorkspaceChat.tsx", import.meta.url),
  "utf8",
);
const chatInputFooter = readFileSync(
  new URL("../src/components/ChatInputFooter.tsx", import.meta.url),
  "utf8",
);
const embeddedChat = readFileSync(
  new URL("../src/components/EmbeddedChat.tsx", import.meta.url),
  "utf8",
);
const css = readFileSync(new URL("../src/index.css", import.meta.url), "utf8");

test("workspace agents use the shared inline mention token", () => {
  assert.match(
    workspaceChat,
    /const mentionOptions = useMemo<MentionOption\[\]>/,
  );
  assert.match(
    workspaceChat,
    /mentions=\{isTaskSession \? \[\] : mentionOptions\}/,
  );
  assert.match(
    workspaceChat,
    /selectedMentions=\{isTaskSession \? \[\] : selectedMentions\}/,
  );
  assert.match(workspaceChat, /onMentionSelect=\{handleMentionSelect\}/);
  assert.match(workspaceChat, /onMentionRemove=\{handleMentionRemove\}/);
});

test("workspace chat has no separate action-row mention chip", () => {
  assert.doesNotMatch(workspaceChat, /beforeTextarea=\{/);
  assert.doesNotMatch(workspaceChat, /workspace-chat-agent-menu/);
  assert.doesNotMatch(workspaceChat, /component\.workspace_chat\.and_times/);
});

test("workspace mention stays a routing control when sent", () => {
  assert.match(workspaceChat, /stripWorkspaceAgentMention/);
  assert.match(
    workspaceChat,
    /text = stripWorkspaceAgentMention\(text, resolvedAgent\.name\)/,
  );
});

test("shared inline mentions support adjacent keyboard deletion", () => {
  assert.match(chatInputFooter, /function adjacentInlineTokenForDeletion/);
  assert.match(
    chatInputFooter,
    /e\.key === "Backspace" \|\| e\.key === "Delete"/,
  );
  assert.match(chatInputFooter, /removeMentionToken\(mention\)/);
});

test("inline mention is one line with prefix, avatar, and name only", () => {
  assert.match(chatInputFooter, /prefix\.textContent = "@"/);
  assert.match(chatInputFooter, /strong\.textContent = part\.mention\.name/);
  assert.match(chatInputFooter, /const avatarRoot = createRoot\(badge\)/);
  assert.match(
    chatInputFooter,
    /<UserAvatar[\s\S]*?name=\{part\.mention\.name\}[\s\S]*?type=\{part\.mention\.type\}[\s\S]*?seed=\{part\.mention\.avatarSeed \|\| part\.mention\.id\}[\s\S]*?size=\{18\}/,
  );
  assert.doesNotMatch(
    chatInputFooter,
    /badge\.textContent = part\.mention\.name\.charAt/,
  );
  assert.doesNotMatch(chatInputFooter, /small\.textContent = part\.mention\.type/);
  assert.match(
    css,
    /\.chat-composer-inline-token--mention \{[\s\S]*?border: 0;[\s\S]*?background: transparent;/,
  );
  assert.match(
    css,
    /\.chat-composer-inline-token--mention \.chat-composer-inline-avatar \{[\s\S]*?width: 18px;[\s\S]*?height: 18px;/,
  );
});

test("sent message mentions reuse the avatar and compact inline treatment", () => {
  assert.match(
    embeddedChat,
    /className="chat-message-inline-mention-prefix">@<\/span>/,
  );
  assert.match(
    embeddedChat,
    /<UserAvatar[\s\S]*?name=\{part\.mention\.name\}[\s\S]*?avatarUrl=\{part\.mention\.avatarUrl\}[\s\S]*?type=\{part\.mention\.type\}[\s\S]*?seed=\{part\.mention\.avatarSeed \|\| part\.mention\.id\}[\s\S]*?size=\{18\}/,
  );
  assert.doesNotMatch(
    embeddedChat,
    /part\.mention\.name\.charAt\(0\)\.toUpperCase\(\)/,
  );
  assert.doesNotMatch(
    embeddedChat,
    /<small>\{part\.mention\.type\}<\/small>/,
  );
  assert.match(
    css,
    /\.chat-message-inline-token--mention \{[\s\S]*?border: 0;[\s\S]*?background: transparent;/,
  );
});
