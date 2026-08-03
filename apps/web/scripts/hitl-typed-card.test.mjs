#!/usr/bin/env node
/**
 * The frontend half of the 15-approvals incident.
 *
 * `rewriteInternalPrompt` used to match "approval before dispatching" and
 * replace the WHOLE sentence with one fixed string, so a card that actually
 * said "Manor could not reach the browser on your computer" rendered as
 * "This step needs your approval before it runs" — with an Approve button
 * under it, which the operator pressed fifteen times.
 *
 * Three things must hold now:
 *   - a card carrying a typed payload renders from the payload;
 *   - an `error` card offers no Approve / Always / Reject;
 *   - a card carrying no payload renders exactly as it did before, because
 *     those cards are still in flight.
 *
 * The card assertions render the real component through react-dom's static
 * renderer rather than grepping the source, so disabling a branch fails here.
 */
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { writeFile, rm } from "node:fs/promises";
import { test, after } from "node:test";
import { build } from "esbuild";

// ChatActionCard's module graph touches the browser at import time (the i18n
// module reads the stored locale). Shim before importing the bundle.
globalThis.localStorage = globalThis.localStorage || {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
};
globalThis.window = globalThis.window || globalThis;

async function bundle(contents, { toFile } = {}) {
  const built = await build({
    stdin: {
      contents,
      loader: "tsx",
      resolveDir: new URL(".", import.meta.url).pathname,
    },
    bundle: true,
    format: "esm",
    platform: "browser",
    write: false,
    logLevel: "silent",
    define: { "process.env.NODE_ENV": '"production"' },
    loader: { ".css": "empty", ".png": "empty", ".svg": "text" },
  });
  const text = built.outputFiles[0].text;
  if (!toFile) {
    return import(
      `data:text/javascript;base64,${Buffer.from(text).toString("base64")}`
    );
  }
  await writeFile(toFile, text);
  return import(toFile.href ?? toFile);
}

const { friendlyApprovalDescription, isErrorHitlCard, structuredApprovalCopy } =
  await bundle(`
    export {
      friendlyApprovalDescription,
      isErrorHitlCard,
      structuredApprovalCopy,
    } from "../src/lib/approvalCopy.ts";
  `);

// A real React render needs a file URL: the bundle is far past the size where
// a data: URL stays debuggable.
const cardBundlePath = new URL("./.hitl-typed-card.bundle.mjs", import.meta.url);
const { renderCard } = await bundle(
  `
    import React from "react";
    import { renderToStaticMarkup } from "react-dom/server.browser";
    import ChatActionCard from "../src/components/ui/ChatActionCard.tsx";
    export function renderCard(action) {
      return renderToStaticMarkup(
        React.createElement(ChatActionCard, { action, onResolve: () => {} }),
      );
    }
  `,
  { toFile: cardBundlePath },
);
after(() => rm(cardBundlePath, { force: true }));

/** The card the dispatcher posts when the user's local worker is offline. */
const OFFLINE_CLI_PAYLOAD = {
  what_happened: "Manor could not reach the browser on your computer.",
  why: "the Chrome MCP reported no paired local worker online.",
  action_to_take:
    "Start the local worker on the computer where you're signed in, then retry.",
  action_link: "/integrations",
  is_transient: true,
};

/** The internal sentence the approval gate synthesizes for an untyped card. */
const INTERNAL_PROMPT =
  "Step requires operator approval before dispatching 'workspace.file.write'.";

function buttonLabels(html) {
  return [...html.matchAll(/<button[^>]*>([^<]*)<\/button>/g)].map((m) => m[1]);
}

/* ── copy ── */

test("a typed error payload renders the real failure, not the approval sentence", () => {
  const copy = structuredApprovalCopy(OFFLINE_CLI_PAYLOAD);
  assert.equal(copy.headline, OFFLINE_CLI_PAYLOAD.what_happened);
  assert.equal(copy.detail, OFFLINE_CLI_PAYLOAD.why);
  assert.equal(copy.actionToTake, OFFLINE_CLI_PAYLOAD.action_to_take);
  assert.equal(copy.actionLink, "/integrations");

  const description = friendlyApprovalDescription({
    prompt: INTERNAL_PROMPT,
    action: "workspace.file.write",
    hitlType: "error",
    payload: OFFLINE_CLI_PAYLOAD,
  });
  assert.match(description, /could not reach the browser/);
  assert.doesNotMatch(description, /needs your approval/i);
});

test("the payload wins even over the workspace.file.* short-circuit", () => {
  // That branch returns before the prompt is ever consulted; a typed payload
  // has to be checked earlier still, or a file-write failure silently renders
  // as "Modify files in this workspace".
  const description = friendlyApprovalDescription({
    prompt: INTERNAL_PROMPT,
    action: "workspace.file.write",
    paths: ["notes/schedule.md"],
    hitlType: "error",
    payload: OFFLINE_CLI_PAYLOAD,
  });
  assert.match(description, /could not reach the browser/);
});

test("a card with no payload renders exactly as it did before", () => {
  assert.equal(structuredApprovalCopy(undefined), null);
  assert.equal(structuredApprovalCopy(null), null);
  assert.equal(structuredApprovalCopy({}), null);

  // Verified byte-identical against the pre-change module across 5472
  // untyped-card input combinations; these are the shapes that actually ship.
  assert.equal(
    friendlyApprovalDescription({ prompt: INTERNAL_PROMPT }),
    "Needs your approval to save a file.",
  );
  assert.equal(
    friendlyApprovalDescription({
      prompt: "Step requires operator approval before dispatching 'subagent'.",
    }),
    "Needs your approval to run this step.",
  );
  assert.equal(
    friendlyApprovalDescription({
      prompt:
        "High-risk step needs one-time operator approval before dispatching 'email.send'.",
    }),
    "Needs your approval to send a message — this is a high-impact action.",
  );
  assert.equal(
    friendlyApprovalDescription({
      prompt:
        "Step 'save_schedule_file' requires operator approval before dispatching 'workspace.file.write'.",
    }),
    "Needs your approval to save the schedule file.",
  );
  assert.equal(
    friendlyApprovalDescription({ prompt: "Run cli command", action: "cli.exec" }),
    "Run a command",
  );
});

test("redaction of ids no longer swallows real content around them", () => {
  // Not one of the gate's pure templates: the trailing sentence is a real
  // failure and must survive. The step key and the tool id must not.
  const description = friendlyApprovalDescription({
    prompt:
      "Step 'save_schedule_file' requires operator approval before dispatching "
      + "'workspace.file.write'. Chrome on your computer never answered.",
  });
  assert.match(description, /Chrome on your computer never answered\./);
  assert.doesNotMatch(description, /save_schedule_file/);
  assert.doesNotMatch(description, /workspace\.file\.write/);
});

test("isErrorHitlCard only fires on the error type", () => {
  assert.equal(isErrorHitlCard("error"), true);
  assert.equal(isErrorHitlCard("ERROR"), true);
  assert.equal(isErrorHitlCard("authorize"), false);
  assert.equal(isErrorHitlCard(undefined), false);
  assert.equal(isErrorHitlCard(null), false);
});

/* ── the rendered card ── */

test("the offline-worker card says what broke, what to do, and offers no Approve", () => {
  const html = renderCard({
    kind: "governance_approval",
    hitl_type: "error",
    task_id: "T123",
    // Posted with the approval vocabulary on purpose: an error card must
    // refuse it rather than pass it through.
    options: ["approve", "always_approve", "reject"],
    prompt: INTERNAL_PROMPT,
    payload: OFFLINE_CLI_PAYLOAD,
  });

  assert.match(html, /Manor could not reach the browser on your computer\./);
  assert.match(html, /Start the local worker on the computer where you&#x27;re signed in/);
  assert.match(html, /href="\/integrations"/);
  assert.match(html, /href="\/tasks\/T123"/);
  assert.doesNotMatch(html, /needs your approval/i);

  const labels = buttonLabels(html);
  assert.deepEqual(labels, ["Retry", "Cancel"]);
  for (const forbidden of ["Approve", "Always", "Reject"]) {
    assert.equal(
      labels.includes(forbidden),
      false,
      `an error card must not offer "${forbidden}"`,
    );
  }
});

test("an error card with no payload still refuses to ask for approval", () => {
  const html = renderCard({
    kind: "governance_approval",
    hitl_type: "error",
    options: ["approve", "always_approve", "reject"],
    prompt: "The worker reported no failure detail.",
  });
  assert.match(html, /The worker reported no failure detail\./);
  assert.deepEqual(buttonLabels(html), ["Retry", "Cancel"]);
});

test("an authorize card is untouched: same copy, same three buttons", () => {
  const html = renderCard({
    kind: "governance_approval",
    hitl_type: "authorize",
    payload: {},
    task_id: "T456",
    options: ["approve", "always_approve", "reject"],
    prompt: INTERNAL_PROMPT,
    action: "workspace.file.write",
    tool: "subagent",
  });
  assert.match(html, /Needs your approval to save a file\./);
  assert.deepEqual(buttonLabels(html), ["Approve", "Always", "Reject"]);
  assert.match(html, /href="\/tasks\/T456"/);
});

test("a non-error typed card also renders from its payload", () => {
  // `authorize` still asks for permission — three buttons — but when it
  // carries a payload the card says what it is asking about in the request's
  // own words rather than rewriting the gate's internal sentence.
  const html = renderCard({
    kind: "governance_approval",
    hitl_type: "authorize",
    task_id: "T789",
    options: ["approve", "always_approve", "reject"],
    prompt: INTERNAL_PROMPT,
    action: "workspace.file.write",
    payload: {
      action_description: "Overwrite notes/schedule.md with this week's plan.",
      why: "This step writes to a file you marked as protected.",
    },
  });
  assert.match(html, /Overwrite notes\/schedule\.md with this week&#x27;s plan\./);
  assert.match(html, /marked as protected/);
  assert.doesNotMatch(html, /Needs your approval to save a file/);
  assert.deepEqual(buttonLabels(html), ["Approve", "Always", "Reject"]);
  assert.match(html, /href="\/tasks\/T789"/);
});

test("a pre-type-system card renders as it always did, link included", () => {
  // No hitl_type, no payload, no task_id — exactly what is in flight today.
  const html = renderCard({
    kind: "governance_approval",
    options: ["approve", "always_approve", "reject"],
    prompt: INTERNAL_PROMPT,
    action: "workspace.file.write",
    tool: "subagent",
  });
  assert.match(html, /Needs your approval to save a file\./);
  assert.deepEqual(buttonLabels(html), ["Approve", "Always", "Reject"]);
  assert.doesNotMatch(html, /chat-hitl-origin-link/);
});

/* ── "Always" is the user's to give ── */

test("a publish-class card renders the Always button like any other", () => {
  // A capability tier that withheld "Always" from publish/email/message
  // shipped briefly and was rejected. The card renders the vocabulary the
  // server posted; only `never_allow` blocks, and it posts no card at all.
  const html = renderCard({
    kind: "governance_approval",
    hitl_type: "authorize",
    task_id: "T900",
    options: ["approve", "always_approve", "reject"],
    prompt: "Publish the launch post to LinkedIn?",
    action: "social_post.publish",
  });
  assert.deepEqual(buttonLabels(html), ["Approve", "Always", "Reject"]);
});

test("a card with no options falls back to the three-button vocabulary", () => {
  const html = renderCard({
    kind: "governance_approval",
    hitl_type: "authorize",
    prompt: "Publish the launch post to LinkedIn?",
    action: "social_post.publish",
  });
  assert.deepEqual(buttonLabels(html), ["Approve", "Always", "Reject"]);
});

/* ── §4.6 the review card says what it is approving ── */

/** What `operation_review_hitl_payload` produces for a draft whose
 *  `rules.replace` drops the workspace's hard block on refunds. */
const HARD_BLOCK_REMOVAL_PAYLOAD = {
  diff: {
    changed_keys: ["rules"],
    added: [],
    removed: [],
    modified: ["rules"],
    removed_hard_blocks: ["payments.refund", "billing.*"],
  },
  why:
    "This draft REMOVES hard blocks that currently cannot be approved around "
    + "at all: payments.refund, billing.*.",
};

test("a workspace-operation review card names the hard blocks being removed", () => {
  const html = renderCard({
    kind: "workspace_operation_review",
    hitl_type: "review",
    draft_id: "D1",
    // Posted with the three-button vocabulary on purpose: a review has no
    // standing version, and the card drops "Always" whatever the blob says.
    options: ["approve", "always_approve", "reject"],
    prompt: "Apply this workspace operation draft?",
    payload: HARD_BLOCK_REMOVAL_PAYLOAD,
    operation: {
      kind: "workspace_operation_review",
      draft_id: "D1",
      changed_keys: ["rules"],
      summary: "Review workspace runtime changes: rules.",
      validation: { errors: [], warnings: [] },
      patches: [{ op: "rules.replace" }],
      removed_hard_blocks: ["payments.refund", "billing.*"],
    },
  });

  // The patterns themselves, not a count and not a category name.
  assert.match(html, /payments\.refund/);
  assert.match(html, /billing\.\*/);
  // And what it means, in the alert region so it is announced.
  assert.match(html, /role="alert"/);
  assert.match(html, /removes hard blocks/i);
  assert.match(html, /with no approval standing in the way/i);

  // A review is a verdict on THIS diff — never a standing grant.
  const labels = buttonLabels(html);
  assert.equal(labels.includes("Always"), false);
  assert.deepEqual(labels, ["Approve", "Reject"]);
});

test("a review card with no hard-block removal raises no alarm", () => {
  const html = renderCard({
    kind: "workspace_operation_review",
    hitl_type: "review",
    draft_id: "D2",
    options: ["approve", "reject"],
    payload: {
      diff: {
        changed_keys: ["goals"],
        added: ["goals"],
        removed: [],
        modified: [],
        removed_hard_blocks: [],
      },
      why: "These changes take effect workspace-wide once applied: goals.",
    },
    operation: {
      kind: "workspace_operation_review",
      draft_id: "D2",
      changed_keys: ["goals"],
      summary: "Review workspace runtime changes: goals.",
      validation: { errors: [], warnings: [] },
      patches: [{ op: "goal.add" }],
      removed_hard_blocks: [],
    },
  });
  assert.doesNotMatch(html, /role="alert"/);
  assert.doesNotMatch(html, /removes hard blocks/i);
  assert.match(html, /take effect workspace-wide/);
});
