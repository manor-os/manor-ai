#!/usr/bin/env node
/**
 * Renders the REAL ChatActionCard against the app's REAL built stylesheet, so
 * the output is what an operator actually sees — not a mockup of it.
 *
 * Shows the before/after of the 15-approvals incident side by side:
 * the untyped card the operator kept pressing Approve on, and the typed
 * `error` card the same failure produces now.
 *
 * Output: scripts/.hitl-card-preview.html  (gitignored build artifact)
 */
import { Buffer } from "node:buffer";
import { readFile, writeFile, rm } from "node:fs/promises";
import { readdirSync } from "node:fs";
import { build } from "esbuild";

globalThis.localStorage = globalThis.localStorage || {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
};
globalThis.window = globalThis.window || globalThis;

const here = new URL(".", import.meta.url);

async function bundleToFile(contents, toFile) {
  const built = await build({
    stdin: { contents, loader: "tsx", resolveDir: here.pathname },
    bundle: true,
    format: "esm",
    platform: "browser",
    write: false,
    logLevel: "silent",
    define: { "process.env.NODE_ENV": '"production"' },
    loader: { ".css": "empty", ".png": "empty", ".svg": "text" },
  });
  await writeFile(toFile, built.outputFiles[0].text);
  return import(toFile.href ?? toFile);
}

const bundlePath = new URL("./.hitl-card-preview.bundle.mjs", here);
const { renderCard } = await bundleToFile(
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
  bundlePath,
);

// The exact failure from the incident: the operator's local worker was not
// running, so Chrome control was unavailable and nothing was ever published.
const REAL_MESSAGE =
  "Stopped before any LinkedIn publish attempt because the Chrome MCP " +
  "reported no paired worker online.";

const typedErrorCard = renderCard({
    kind: "governance_approval",
    hitl_type: "error",
    step_id: "01KYTDCEPGN69TGCYKTNT398BS",
    task_id: "01KYDZQY92KWMM0776YMGJZ92D",
    plan_id: "01KYTDCEP02G4JR5EPXS5SY5HB",
    approval_request_id: "01KYTEKTB5ZSVC0G81SJHX9QHC",
    action: "social_post.publish",
    prompt: "Manor could not reach the browser on your computer.",
    options: ["retry", "cancel"],
    payload: {
      what_happened: "Manor could not reach the browser on your computer.",
      why: REAL_MESSAGE,
      action_to_take:
        "Start the Manor worker on the computer where you're signed in, then retry.",
      action_link: "/integrations",
      is_transient: true,
    },
});

// What the same failure rendered as before this work — the sentence the
// operator saw fifteen times, with an Approve button under it.
const legacyCard = renderCard({
    kind: "governance_approval",
    step_id: "01KYTDCEPGN69TGCYKTNT398BS",
    plan_id: "01KYTDCEP02G4JR5EPXS5SY5HB",
    action: "social_post.publish",
    prompt:
      "High-risk step needs one-time operator approval before dispatching 'action'.",
    options: ["approve", "always_approve", "reject"],
});

const cssFile = readdirSync(new URL("../dist/assets", here))
  .filter((f) => f.startsWith("index-") && f.endsWith(".css"))
  .sort()[0];
const css = await readFile(new URL(`../dist/assets/${cssFile}`, here), "utf8");

const page = `<!doctype html>
<html><head><meta charset="utf-8"><style>${css}</style>
<style>
  body { margin:0; padding:32px; background:#f6f6f5; font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }
  .wrap { max-width: 760px; margin:0 auto; display:flex; flex-direction:column; gap:36px; }
  .panel h2 { font-size:13px; font-weight:600; letter-spacing:.06em; text-transform:uppercase; margin:0 0 4px; }
  .panel p.note { font-size:13px; color:#6b6b6b; margin:0 0 14px; }
  .before h2 { color:#b42318; }
  .after h2 { color:#067647; }
  .card-host { background:#fff; border:1px solid #e5e5e3; border-radius:14px; padding:18px 20px; }
</style></head>
<body><div class="wrap">
  <div class="panel before">
    <h2>Before — what the operator saw 15 times</h2>
    <p class="note">同一个失败,固定文案 + Approve 按钮。真实原因(本地 worker 没运行)完全不可见。</p>
    <div class="card-host">${legacyCard}</div>
  </div>
  <div class="panel after">
    <h2>After — the same failure, typed as an error</h2>
    <p class="note">说清发生了什么 / 为什么 / 该做什么,给可点链接,并且没有 Approve 按钮。</p>
    <div class="card-host">${typedErrorCard}</div>
  </div>
</div></body></html>`;

const out = new URL("./.hitl-card-preview.html", here);
await writeFile(out, page);
await rm(bundlePath, { force: true });
console.log(out.pathname);
