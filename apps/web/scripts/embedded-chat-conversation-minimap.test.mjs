import { readFile } from "node:fs/promises";
import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.resolve(scriptDir, "..");

const component = await readFile(
  path.join(webRoot, "src/components/EmbeddedChat.tsx"),
  "utf8",
);
const scrollRail = await readFile(
  path.join(webRoot, "src/components/chat/ChatScrollRail.tsx"),
  "utf8",
);
const workspaceChat = await readFile(
  path.join(webRoot, "src/components/WorkspaceChat.tsx"),
  "utf8",
);
const css = await readFile(path.join(webRoot, "src/index.css"), "utf8");

assert.match(
  component,
  /import ChatScrollRail,[\s\S]*type ChatScrollRailMarker/,
  "EmbeddedChat should import the reusable conversation rail and marker type",
);
assert.match(
  scrollRail,
  /aria-current=\{isActive \? "location"/,
  "The active conversation rail marker should expose aria-current for accessible state",
);
assert.match(
  component,
  /data-chat-message-index=\{i\}/,
  "Messages should keep stable indexes for conversation rail sampling",
);
assert.match(
  component,
  /const chatScrollRailMarkers = useMemo<ChatScrollRailMarker\[\]>/,
  "Embedded chat should derive structured conversation rail markers",
);
assert.match(
  component,
  /<ChatScrollRail[\s\S]*containerRef=\{chatBodyRef\}[\s\S]*markers=\{chatScrollRailMarkers\}/,
  "Embedded chat should render the conversation rail against the chat body",
);
assert.match(
  scrollRail,
  /el\.addEventListener\("scroll", scheduleUpdate/,
  "Conversation rail should observe chat body scroll",
);
assert.match(
  scrollRail,
  /el\.scrollTo\(\{[\s\S]*behavior: "smooth"/,
  "Conversation rail markers should jump smoothly to the matching position",
);
assert.match(
  scrollRail,
  /function getMarkerTarget[\s\S]*data-chat-scroll-marker-id[\s\S]*data-chat-message-index/,
  "rail marker jumps should resolve rendered message nodes before using a ratio fallback",
);
assert.match(
  scrollRail,
  /function targetScrollTopForMarker[\s\S]*getMarkerTarget[\s\S]*container\.scrollTop \+ targetRect\.top - containerRect\.top/,
  "rail marker jumps should remeasure the selected message at click time",
);
assert.match(
  scrollRail,
  /const MARKER_TARGET_TOP_OFFSET_PX = 16/,
  "rail marker jumps should keep a small readable offset above the selected message",
);
assert.match(
  scrollRail,
  /targetTop - MARKER_TARGET_TOP_OFFSET_PX/,
  "clicking a rail marker should align the selected message near the top of the chat viewport",
);
assert.doesNotMatch(
  scrollRail,
  /container\.clientHeight - targetHeight/,
  "rail marker jumps should not center the target message",
);
assert.match(
  scrollRail,
  /const targetTop = targetScrollTopForMarker\([\s\S]*entry\.marker[\s\S]*entry\.sourceIndex/,
  "clicking a rail marker should use the latest rendered message position",
);
assert.match(
  scrollRail,
  /const MAX_MARKERS = 72[\s\S]*const MARKER_GAP_PX = 10[\s\S]*function sampleMarkers/,
  "Conversation rail should sample long threads into a dense centered rail",
);
assert.match(
  scrollRail,
  /className=\{`chat-scroll-rail__marker chat-scroll-rail__marker--\$\{[\s\S]*marker\.tone \|\| "assistant"/,
  "Conversation rail should render tone-specific message markers",
);
assert.match(
  workspaceChat,
  /data-chat-scroll-marker-id=\{markerId \|\| msg\.id\}/,
  "workspace persisted chat rows should expose stable rail marker targets",
);
assert.match(
  workspaceChat,
  /data-chat-scroll-marker-id=\{item\.key\}/,
  "workspace local chat rows should expose stable rail marker targets",
);
assert.match(
  css,
  /\.chat-scroll-rail-host > \.embedded-chat-body \{[\s\S]*padding-left: 64px/,
  "Embedded chat body should reserve compact room for the left conversation rail",
);
assert.match(
  css,
  /\.chat-scroll-rail \{[\s\S]*top: 50%[\s\S]*left: 10px[\s\S]*height: min\(560px, calc\(100% - 64px\)\)/,
  "Conversation rail should center a dense short-tick stack near the left edge",
);
assert.match(
  css,
  /\.chat-scroll-rail \{[\s\S]*transform: translateY\(-50%\)/,
  "Conversation rail should be vertically centered instead of stretched full-height",
);
assert.match(
  scrollRail,
  /style=\{\{ height: `\$\{railHeight\}px` \}\}/,
  "Conversation rail should use a computed compact height from marker count",
);
assert.match(
  css,
  /\.chat-scroll-rail__marker \{[\s\S]*width: 10px[\s\S]*height: 2px/,
  "Conversation rail markers should default to fine horizontal ticks",
);
assert.match(
  css,
  /\.chat-scroll-rail__marker--artifact \{[\s\S]*background: rgba\(28, 25, 23, 0\.17\)/,
  "Artifact markers should stay neutral in the quiet default rail",
);
assert.match(
  scrollRail,
  /className="embedded-chat-conversation-preview chat-scroll-rail__preview"/,
  "Rail markers should expose the shared hover preview component",
);
assert.doesNotMatch(
  scrollRail,
  /className="embedded-chat-conversation-preview__meta"/,
  "Rail preview should not render a file/source metadata footer",
);
assert.doesNotMatch(
  scrollRail,
  /className="embedded-chat-conversation-preview__source-icon"/,
  "Rail preview should not render a file/source icon tag",
);
assert.doesNotMatch(
  scrollRail,
  /className="embedded-chat-conversation-preview__source-title"/,
  "Rail preview should not render a file/source label",
);
assert.doesNotMatch(
  scrollRail,
  /marker\.sourceCount[\s\S]*embedded-chat-conversation-preview__source-more/,
  "Rail preview should not render a +N source count",
);
assert.match(
  css,
  /\.embedded-chat-conversation-preview \{[^}]*background: var\(--surface-panel\)/,
  "Rail preview should use an opaque panel background",
);
assert.match(
  css,
  /\.embedded-chat-conversation-preview \{[^}]*width: min\(340px, calc\(100vw - 390px\)\)[^}]*max-height: 156px[^}]*padding: 10px 12px/,
  "Rail preview should use a compact refined card size",
);
assert.match(
  css,
  /\.embedded-chat-conversation-preview__title \{[^}]*font-size: 13px[^}]*overflow-wrap: anywhere[^}]*-webkit-line-clamp: 2/,
  "Rail preview title should stay compact and clamp long questions",
);
assert.match(
  css,
  /\.embedded-chat-conversation-preview__body \{[^}]*font-size: 12px[^}]*overflow-wrap: anywhere[^}]*-webkit-line-clamp: 3/,
  "Rail preview body should clamp long replies without overflowing",
);
assert.match(
  css,
  /\.chat-scroll-rail__item:has\(\.chat-scroll-rail__button:hover\)[^}]*width: 28px/,
  "Rail hover should expand one marker into a restrained preview line",
);
assert.match(
  css,
  /\.chat-scroll-rail__button:hover \+ \.embedded-chat-conversation-preview/,
  "Rail hover should reveal the adjacent conversation preview",
);
assert.match(
  css,
  /\.chat-scroll-rail__viewport \{[\s\S]*pointer-events: none/,
  "The viewport indicator must not steal marker clicks",
);
assert.match(
  css,
  /\.chat-scroll-rail__item \{[\s\S]*pointer-events: auto/,
  "Only marker items should accept pointer events",
);
assert.match(
  css,
  /@media \(max-width: 1024px\)[\s\S]*\.chat-scroll-rail \{[\s\S]*display: none[\s\S]*\.chat-scroll-rail-host > \.embedded-chat-body \{[\s\S]*padding-left: 24px/,
  "Conversation rail should collapse on narrow layouts",
);

console.log("embedded chat conversation rail checks passed");
