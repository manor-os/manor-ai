#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const appLayout = readFileSync(
  new URL("../src/layouts/AppLayout.tsx", import.meta.url),
  "utf8",
);
const embeddedChat = readFileSync(
  new URL("../src/components/EmbeddedChat.tsx", import.meta.url),
  "utf8",
);
const floatingChat = readFileSync(
  new URL("../src/components/FloatingChat.tsx", import.meta.url),
  "utf8",
);
const css = readFileSync(new URL("../src/index.css", import.meta.url), "utf8");
const locales = ["en", "zh", "es"].map((locale) =>
  readFileSync(
    new URL(`../src/lib/i18n/${locale}.ts`, import.meta.url),
    "utf8",
  ),
);

test("the pinned Manor AI row expands personal chat sessions", () => {
  assert.match(appLayout, /const \[manorSessionsOpen, setManorSessionsOpen\]/);
  assert.match(appLayout, /aria-expanded=\{manorSessionsOpen\}/);
  assert.match(appLayout, /aria-controls="manor-chat-sessions"/);
  assert.match(
    appLayout,
    /!conversation\.agent_id && !conversation\.workspace_id/,
  );
  assert.doesNotMatch(appLayout, /manorChatSessions\.slice\(0, 8\)/);
  assert.match(appLayout, /filteredManorChatSessions\.map/);
  assert.match(appLayout, /className="chat-sidebar-global-search"/);
  assert.match(appLayout, /page\.app_layout\.search_chats_and_agents/);
  assert.match(appLayout, /const conversationSearch = convSearchQuery/);
  assert.match(appLayout, /filteredManorChatSessions\[0\][\s\S]*?openManorSession/);
  assert.match(appLayout, /page\.app_layout\.no_matching_chats_or_agents/);
  assert.doesNotMatch(appLayout, /manorSessionSearch/);
  assert.doesNotMatch(appLayout, /className="manor-session-toolbar"/);
});

test("selecting one Manor session deep-links and activates that exact chat", () => {
  assert.match(appLayout, /setActiveConvId\(conversationId\)/);
  assert.match(appLayout, /setActiveConvType\("manor"\)/);
  assert.match(
    appLayout,
    /navigate\(`\/chat\?conversation=\$\{encodeURIComponent\(conversationId\)\}`\)/,
  );
  assert.match(appLayout, /aria-current=\{isActive \? "page" : undefined\}/);
});

test("the Manor chat header owns new while the session panel owns rename and delete", () => {
  assert.match(appLayout, /const startNewManorSession = \(\) =>/);
  assert.match(appLayout, /`manor-new:\$\{Date\.now\(\)\}`/);
  assert.match(
    appLayout,
    /<EmbeddedChat[\s\S]*?onNewConversation=\{[\s\S]*?startNewManorSession/,
  );
  assert.match(
    embeddedChat,
    /className="embedded-chat-new-session"[\s\S]*?component\.session_switcher\.new_chat_2/,
  );
  assert.doesNotMatch(appLayout, /className="manor-session-new"/);
  assert.match(appLayout, /<Dropdown/);
  assert.match(appLayout, /api\.chat\.renameConversation/);
  assert.match(appLayout, /api\.chat\.deleteConversation/);
  assert.match(appLayout, /<ConfirmDialog/);
  assert.match(appLayout, /component\.session_switcher\.rename_chat/);
  assert.match(appLayout, /component\.session_switcher\.delete_chat/);
  assert.match(
    appLayout,
    /<HoverMarqueeText[\s\S]*?className="manor-session-title"[\s\S]*?conversation\.title/,
  );
  assert.match(appLayout, /<IconMoreHorizontal size=\{18\} \/>/);
  assert.match(css, /\.manor-session-more[\s\S]*?opacity:\s*1/);
  assert.match(
    css,
    /\.embedded-chat-new-session\.btn-manor-ghost[\s\S]*?width:\s*40px[\s\S]*?height:\s*40px/,
  );
});

test("running Manor sessions replace the menu dots with the shared activity orb until hover", () => {
  assert.match(appLayout, /inferAgentActivity/);
  assert.match(appLayout, /import \{ useChatStreamStore \}/);
  assert.match(
    appLayout,
    /if \(!session\.streaming \|\| !session\.convId\) return \[\]/,
  );
  assert.match(appLayout, /inferAgentActivity\(latestAssistantMessage\)/);
  assert.match(
    appLayout,
    /runningManorSessionActivities\.get\([\s\S]*?conversation\.id/,
  );
  assert.match(appLayout, /manor-session-more--running/);
  assert.match(
    appLayout,
    /<AgentActivityOrb[\s\S]*?activity=\{[\s\S]*?runningActivity \|\| "working"[\s\S]*?iconOnly/,
  );
  assert.match(appLayout, /manor-session-more__dots/);
  assert.match(
    css,
    /\.manor-session-more--running \.manor-session-more__orb[\s\S]*?opacity:\s*1/,
  );
  assert.match(
    css,
    /\.manor-session-row:hover[\s\S]*?\.manor-session-more--running[\s\S]*?\.manor-session-more__dots[\s\S]*?opacity:\s*1/,
  );
});

test("a new Manor draft resolves to its real conversation URL", () => {
  assert.match(embeddedChat, /id\.startsWith\("manor-new:"\)/);
  assert.match(embeddedChat, /onConversationResolved\?\.\(newConvId\)/);
  assert.match(appLayout, /onConversationResolved=\{/);
  assert.match(
    appLayout,
    /handleManorConversationResolved[\s\S]*?invalidateQueries\(\{ queryKey: \["conversations"\] \}\)/,
  );
  assert.match(appLayout, /navigate\(`\/chat\?conversation=/);
});

test("starting a new Manor chat cannot be replaced by a stale auto-resume", () => {
  assert.match(
    embeddedChat,
    /isNewManorConversationId\(conversationId\)[\s\S]*?currentSessionKeyRef\.current = sessionKey/,
  );
  assert.match(
    embeddedChat,
    /Auto-resume most recent conversation[\s\S]*?let cancelled = false[\s\S]*?if \(cancelled\) return undefined/,
  );
  assert.match(
    embeddedChat,
    /loadConversationMessages\(latest\.id, \{[\s\S]*?onlyIfStillCurrent: true/,
  );
  assert.match(embeddedChat, /return \(\) => \{\s*cancelled = true;\s*\}/);
});

test("a running Manor session can continue in the background while another session opens", () => {
  assert.doesNotMatch(
    embeddedChat,
    /Reset when conversation changes[\s\S]*?if \(streamingRef\.current\) return/,
  );
  assert.match(
    embeddedChat,
    /Rebind the visible conversation while other per-session streams continue in the background/,
  );
  assert.match(
    embeddedChat,
    /if \(currentSessionKeyRef\.current === sessionKey\) \{[\s\S]*?onConversationResolved\?\.\(newConvId\);[\s\S]*?\}/,
  );
  assert.doesNotMatch(
    embeddedChat,
    /if \(currentSessionKeyRef\.current === sessionKey\) \{[\s\S]*?\}[\s\n]*onConversationResolved\?\.\(newConvId\);/,
  );
});

test("the main chat avoids a duplicate session switcher", () => {
  assert.doesNotMatch(embeddedChat, /import SessionSwitcher/);
  assert.doesNotMatch(embeddedChat, /<SessionSwitcher/);
  assert.match(floatingChat, /<SessionSwitcher/);
});

test("the session list has shared loading, empty, error, and keyboard states", () => {
  assert.match(appLayout, /<InlineRowsSkeleton rows=\{3\} dense \/>/);
  assert.match(appLayout, /page\.app_layout\.no_manor_sessions/);
  assert.match(appLayout, /page\.app_layout\.manor_sessions_unavailable/);
  assert.match(
    css,
    /\.manor-session-row-main:focus-visible[\s\S]*?var\(--accent-ring\)/,
  );
  assert.match(
    css,
    /\.manor-session-list[\s\S]*?max-height:\s*248px[\s\S]*?overflow-y:\s*auto/,
  );
  assert.match(css, /\.chat-sidebar-global-search:focus-visible[\s\S]*?var\(--accent-ring\)/);
  assert.match(
    css,
    /@media \(prefers-reduced-motion: reduce\)[\s\S]*?\.manor-session-toggle svg[\s\S]*?transition:\s*none/,
  );
});

test("every fully translated locale names the Manor session states", () => {
  for (const locale of locales) {
    assert.match(locale, /"page\.app_layout\.expand_manor_sessions"/);
    assert.match(locale, /"page\.app_layout\.collapse_manor_sessions"/);
    assert.match(locale, /"page\.app_layout\.no_manor_sessions"/);
    assert.match(locale, /"page\.app_layout\.manor_sessions_unavailable"/);
    assert.match(locale, /"page\.app_layout\.search_manor_sessions"/);
    assert.match(locale, /"page\.app_layout\.search_chats_and_agents"/);
    assert.match(locale, /"page\.app_layout\.no_matching_chats_or_agents"/);
    assert.match(locale, /"page\.app_layout\.running_manor_session_menu"/);
    assert.match(locale, /"page\.app_layout\.no_matching_manor_sessions"/);
    assert.match(locale, /"page\.app_layout\.delete_manor_session_message"/);
    assert.match(locale, /"page\.app_layout\.rename_manor_session_failed"/);
    assert.match(locale, /"page\.app_layout\.delete_manor_session_failed"/);
  }
});
