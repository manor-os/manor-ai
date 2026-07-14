#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const component = await readFile(
  new URL("../src/components/EmbeddedChat.tsx", import.meta.url),
  "utf8",
);
const css = await readFile(new URL("../src/index.css", import.meta.url), "utf8");

assert.match(
  component,
  /function ConversationMinimap\([\s\S]*aria-current=\{isActive \? "location"/,
  "conversation minimap should expose an accessible active marker",
);
assert.match(
  component,
  /data-chat-message-index=\{i\}/,
  "every rendered message should be a minimap destination",
);
assert.match(
  component,
  /body\.addEventListener\("scroll", scheduleConversationMinimapUpdate/,
  "the active marker should follow the real chat scroll container",
);
assert.match(
  component,
  /body\.scrollTo\(\{[\s\S]*behavior: "smooth"/,
  "selecting a marker should smoothly navigate inside the chat body",
);
assert.match(
  component,
  /compactLayout = visibleMarkers\.length <= 32[\s\S]*Math\.min\(18, 520 \/ \(visibleMarkers\.length - 1\)\)/,
  "small conversations should use a compact fixed-gap marker group",
);
assert.match(
  component,
  /embedded-chat-conversation-minimap-item[\s\S]*aria-describedby=\{previewId\}[\s\S]*embedded-chat-conversation-preview/,
  "each marker should own an accessible message preview",
);
assert.match(
  css,
  /\.embedded-chat-conversation-minimap button\.is-active span \{[\s\S]*width: 52px/,
  "the active location should use the longer neutral line from the reference UI",
);
assert.match(
  css,
  /embedded-chat-conversation-minimap-item:has\(button:hover\)[\s\S]*width: 48px[\s\S]*\+ \.embedded-chat-conversation-minimap-item[\s\S]*width: 32px/s,
  "hovering a marker should create a local wave around the selected line",
);
assert.match(
  css,
  /button:hover \+ \.embedded-chat-conversation-preview[\s\S]*visibility: visible/,
  "hovering a marker should reveal its preview without transient React state",
);
assert.match(
  css,
  /\.embedded-chat-conversation-preview \{[\s\S]*box-shadow: var\(--shadow-lg\)/,
  "the hover preview should use the platform's elevated neutral surface",
);
assert.match(
  css,
  /@media \(max-width: 1024px\)[\s\S]*\.embedded-chat-conversation-minimap \{[\s\S]*display: none/,
  "the overlay should stay out of compact chat layouts",
);

console.log("embedded chat conversation minimap checks passed");
