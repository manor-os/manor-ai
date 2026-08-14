#!/usr/bin/env node
/**
 * Workspace chat must render the OTHER HITL channel.
 *
 * Production incident: a user asked the workspace agent to send an email.
 * `mcp__email__send_email` gated correctly and returned a `__hitl__` envelope,
 * `chat_service` recorded it into `messages.metadata->'hitl_requests'`, and the
 * DB shows a pending `authorize` request against `email.send`. The user saw
 * nothing. Two channels carry a blocked action:
 *
 *   pending_action           — governance/step gate  → WorkspaceChat rendered this
 *   metadata.hitl_requests   — tool-call `__hitl__`  → WorkspaceChat ignored this
 *
 * A tool-call approval in a workspace conversation was therefore recorded,
 * badged, and completely unactionable.
 *
 * These assertions render the real `WsMessageRow` through react-dom's static
 * renderer. A source grep would survive deleting the JSX; this does not.
 */
import assert from "node:assert/strict";
import { rm } from "node:fs/promises";
import { test, after } from "node:test";
import { build } from "esbuild";

// WorkspaceChat's module graph touches the browser at import time (i18n reads
// the stored locale, the auth store reads persisted state). Shim first.
globalThis.localStorage = globalThis.localStorage || {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
};
globalThis.sessionStorage = globalThis.sessionStorage || globalThis.localStorage;
globalThis.window = globalThis.window || globalThis;
// The auth store reads an impersonation token off the URL at import time.
globalThis.location = globalThis.location || {
  hash: "",
  search: "",
  pathname: "/",
  href: "http://localhost/",
  origin: "http://localhost",
};
globalThis.history = globalThis.history || {
  replaceState: () => {},
  pushState: () => {},
};
/* Two libraries poke the DOM at *import* time: react-markdown's entity decoder
 * calls `document.createElement`, and react-dom's client build feature-tests
 * attributes on a scratch element. Neither runs during these assertions —
 * rendering goes through `react-dom/server.browser`, which needs no DOM — so a
 * permissive fake element is enough to get the module graph loaded. It stores
 * whatever is set on it and answers every unknown method with a no-op. */
function fakeElement() {
  const store = { innerHTML: "", textContent: "", style: {}, nodeType: 1 };
  return new Proxy(store, {
    get: (target, key) =>
      key in target ? target[key] : typeof key === "string" ? () => {} : undefined,
    set: (target, key, value) => {
      target[key] = value;
      return true;
    },
    has: () => true,
  });
}
globalThis.document = globalThis.document || {
  createElement: fakeElement,
  createElementNS: fakeElement,
  createTextNode: fakeElement,
  documentElement: fakeElement(),
  addEventListener: () => {},
  removeEventListener: () => {},
};
globalThis.addEventListener = globalThis.addEventListener || (() => {});
globalThis.removeEventListener = globalThis.removeEventListener || (() => {});
globalThis.matchMedia =
  globalThis.matchMedia ||
  (() => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }));

// Vite injects `import.meta.env`; node does not, and zustand reads `.DEV` at
// import time. Supply it so the real module graph loads unmodified.
const BUILD_DEFINE = {
  "process.env.NODE_ENV": '"production"',
  "import.meta.env": JSON.stringify({ DEV: false, PROD: true, MODE: "production" }),
};

const bundlePath = new URL("./.workspace-chat-hitl-card.bundle.mjs", import.meta.url);
after(() => rm(bundlePath, { force: true }));

await build({
  stdin: {
    contents: `
      import React from "react";
      import { renderToStaticMarkup } from "react-dom/server.browser";
      import { MemoryRouter } from "react-router-dom";
      import { WsMessageRow } from "../src/components/WorkspaceChat.tsx";

      export function renderRow(msg, props = {}) {
        return renderToStaticMarkup(
          React.createElement(
            MemoryRouter,
            null,
            React.createElement(WsMessageRow, {
              msg,
              subToAgent: new Map(),
              currentUserName: "Lin",
              currentUserId: "U1",
              onResolve: () => {},
              onFeedback: () => {},
              ...props,
            }),
          ),
        );
      }
    `,
    loader: "tsx",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "esm",
  platform: "browser",
  outfile: bundlePath.pathname,
  logLevel: "silent",
  define: BUILD_DEFINE,
  loader: { ".css": "empty", ".png": "empty", ".svg": "text" },
});

const { renderRow } = await import(bundlePath.href);

/* A second bundle renders the same row with `ChatActionCard` swapped for a stub
 * that records the props it is handed. `renderToStaticMarkup` produces markup
 * with no live handlers, and this repo has no DOM harness, so capturing the
 * real `onResolve` the row builds — and calling it — is how a click is proven
 * to reach the right request id rather than assumed to. */
const stubBundlePath = new URL(
  "./.workspace-chat-hitl-card.stub.bundle.mjs",
  import.meta.url,
);
after(() => rm(stubBundlePath, { force: true }));

const cardStubPlugin = {
  name: "stub-chat-action-card",
  setup(pluginBuild) {
    pluginBuild.onResolve({ filter: /ui\/ChatActionCard$/ }, () => ({
      path: "chat-action-card-stub",
      namespace: "stub",
    }));
    pluginBuild.onLoad({ filter: /.*/, namespace: "stub" }, () => ({
      contents: `
        import React from "react";
        export function ApprovalSummary() { return null; }
        export default function ChatActionCard(props) {
          (globalThis.__cardProps ||= []).push(props);
          return React.createElement("div", { "data-card": "stub" });
        }
      `,
      loader: "js",
      resolveDir: new URL(".", import.meta.url).pathname,
    }));
  },
};

await build({
  stdin: {
    contents: `
      import React from "react";
      import { renderToStaticMarkup } from "react-dom/server.browser";
      import { MemoryRouter } from "react-router-dom";
      import { WsMessageRow } from "../src/components/WorkspaceChat.tsx";

      export function cardPropsFor(msg, props = {}) {
        globalThis.__cardProps = [];
        renderToStaticMarkup(
          React.createElement(
            MemoryRouter,
            null,
            React.createElement(WsMessageRow, {
              msg,
              subToAgent: new Map(),
              currentUserName: "Lin",
              currentUserId: "U1",
              onResolve: () => {},
              onFeedback: () => {},
              ...props,
            }),
          ),
        );
        return globalThis.__cardProps;
      }
    `,
    loader: "tsx",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "esm",
  platform: "browser",
  outfile: stubBundlePath.pathname,
  logLevel: "silent",
  define: BUILD_DEFINE,
  loader: { ".css": "empty", ".png": "empty", ".svg": "text" },
  plugins: [cardStubPlugin],
});

const { cardPropsFor } = await import(stubBundlePath.href);

/** The exact prod row: assistant message, no pending_action, the blocked
 *  `email.send` call recorded in metadata. */
function emailMessage(overrides = {}) {
  return {
    id: "M1",
    conversation_id: "C1",
    created_at: "2026-08-04T05:51:41Z",
    body: "I need your approval before sending this email.",
    message_kind: "hitl_request",
    author_kind: "agent",
    author_subscription_id: null,
    refs: null,
    attachments: null,
    meta: {},
    // The channel the workspace surface used to ignore entirely.
    pending_action: null,
    resolved_at: null,
    resolution: null,
    hitl_requests: [
      {
        id: "01KZ87N332J6V9XTSY7KTPBX0E",
        type: "approval",
        prompt: "Step requires operator approval before dispatching 'email.send'.",
        action: "email.send",
        tool: "mcp__email__send_email",
        options: ["approve", "always_approve", "reject"],
      },
    ],
    ...overrides,
  };
}

function buttonLabels(html) {
  // Scoped to the card's own action row. Reading every <button> in the
  // message row made this assert on whatever else the row happens to render
  // — the metadata polish added a timestamp button and three tests went red
  // without the card changing at all. What these tests are about is which
  // decisions the card offers.
  const actions = html.match(/<div class="chat-hitl-actions"[^>]*>([\s\S]*?)<\/div>/);
  const scope = actions ? actions[1] : "";
  return [...scope.matchAll(/<button[^>]*>([^<]*)<\/button>/g)].map((m) => m[1]);
}

test("a tool-call HITL in workspace chat renders an actionable card", () => {
  const html = renderRow(emailMessage());

  // The card exists at all — this is the whole incident.
  assert.match(html, /chat-hitl-card/);
  // And it says what is being asked, not the gate's internal sentence.
  assert.match(html, /Needs your approval to send a message/i);
  // And it can be acted on.
  assert.deepEqual(buttonLabels(html), ["Approve", "Always", "Reject"]);
});

test("resolving the card reaches onHitlAction with THAT request's id", () => {
  // A card whose buttons post nothing is the same bug wearing a hat. Invoke
  // the real `onResolve` the row built and check where it lands.
  const seen = [];
  const [card] = cardPropsFor(emailMessage(), {
    onHitlAction: (hitlId, action) => seen.push([hitlId, action]),
  });
  assert.ok(card, "the row must render an action card for a tool-call HITL");
  assert.equal(card.disabled, false);
  assert.equal(card.resolved, false);
  assert.deepEqual(card.action.options, ["approve", "always_approve", "reject"]);

  card.onResolve("approve");
  assert.deepEqual(seen, [["01KZ87N332J6V9XTSY7KTPBX0E", "approve"]]);

  card.onResolve("reject");
  assert.deepEqual(seen.at(-1), ["01KZ87N332J6V9XTSY7KTPBX0E", "reject"]);
});

test("each card resolves its own id when a message carries several", () => {
  // Binding the handler to the wrong entry would approve the wrong action —
  // silently, and with the user believing they approved the one they clicked.
  const seen = [];
  const cards = cardPropsFor(
    emailMessage({
      hitl_requests: [
        { id: "AAA", type: "approval", prompt: "First?", action: "email.send" },
        { id: "BBB", type: "approval", prompt: "Second?", action: "cli.exec" },
      ],
    }),
    { onHitlAction: (hitlId, action) => seen.push([hitlId, action]) },
  );
  assert.equal(cards.length, 2);
  cards[1].onResolve("approve");
  cards[0].onResolve("reject");
  assert.deepEqual(seen, [["BBB", "approve"], ["AAA", "reject"]]);
});

test("a card is not clickable while a turn is streaming", () => {
  const [card] = cardPropsFor(emailMessage(), { streaming: true });
  assert.equal(card.disabled, true);
});

test("an already-resolved request renders resolved, not a fresh Approve", () => {
  // Re-offering buttons on a decided request invites a double-approve.
  const html = renderRow(
    emailMessage({
      hitl_requests: [
        {
          id: "01KZ87N332J6V9XTSY7KTPBX0E",
          type: "approval",
          prompt: "Step requires operator approval before dispatching 'email.send'.",
          action: "email.send",
          options: ["approve", "always_approve", "reject"],
          resolved: true,
          resolution: "approve",
        },
      ],
    }),
  );
  assert.match(html, /chat-hitl-card/);
  const labels = buttonLabels(html);
  for (const forbidden of ["Approve", "Always", "Reject"]) {
    assert.equal(
      labels.includes(forbidden),
      false,
      `a resolved request must not offer "${forbidden}" again`,
    );
  }
});

test("a message with no hitl_requests renders no card", () => {
  // The channel must not invent cards for ordinary chatter.
  const html = renderRow(emailMessage({ hitl_requests: null }));
  assert.doesNotMatch(html, /chat-hitl-card/);
  assert.deepEqual(buttonLabels(html), []);
});

test("an entry with no id is dropped rather than rendered unresolvable", () => {
  // The reply is keyed on the id; a card without one could never resolve.
  const html = renderRow(
    emailMessage({
      hitl_requests: [{ type: "approval", prompt: "Approve something?" }],
    }),
  );
  assert.doesNotMatch(html, /chat-hitl-card/);
});

test("the pending_action channel still renders — the fix adds, never replaces", () => {
  const html = renderRow(
    emailMessage({
      hitl_requests: null,
      pending_action: {
        kind: "governance_approval",
        options: ["approve", "always_approve", "reject"],
        prompt: "Step requires operator approval before dispatching 'email.send'.",
        action: "email.send",
      },
    }),
  );
  assert.deepEqual(buttonLabels(html), ["Approve", "Always", "Reject"]);
});
