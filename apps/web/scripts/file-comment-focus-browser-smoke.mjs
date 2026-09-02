#!/usr/bin/env node
import assert from "node:assert/strict";
import { build } from "esbuild";
import { chromium } from "@playwright/test";

const bundle = await build({
  stdin: {
    contents: `
      export { updateCommentMarkActiveState } from "../src/lib/markdownCommentMarkers.mjs";
    `,
    loader: "js",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "iife",
  globalName: "ManorFileComments",
  platform: "browser",
  write: false,
  logLevel: "silent",
});

const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
const browser = await chromium.launch({
  headless: true,
  ...(executablePath ? { executablePath } : {}),
});

try {
  const page = await browser.newPage();
  await page.addScriptTag({ content: bundle.outputFiles[0].text });
  const result = await page.evaluate(() => {
    const root = document.createElement("div");
    root.innerHTML = `
      <button class="document-comment-mark is-active" data-comment-id="first" aria-pressed="true">First</button>
      <button class="document-comment-mark" data-comment-id="second" aria-pressed="false">Second</button>
    `;
    document.body.append(root);

    const first = root.querySelector('[data-comment-id="first"]');
    const second = root.querySelector('[data-comment-id="second"]');
    second.focus();
    const focusedNode = document.activeElement;

    window.ManorFileComments.updateCommentMarkActiveState(root, "second");

    return {
      focusPreserved: document.activeElement === focusedNode && focusedNode === second,
      focusedNodePreserved: root.querySelector('[data-comment-id="second"]') === second,
      firstPressed: first.getAttribute("aria-pressed"),
      firstActive: first.classList.contains("is-active"),
      secondPressed: second.getAttribute("aria-pressed"),
      secondActive: second.classList.contains("is-active"),
    };
  });

  assert.deepEqual(result, {
    focusPreserved: true,
    focusedNodePreserved: true,
    firstPressed: "false",
    firstActive: false,
    secondPressed: "true",
    secondActive: true,
  });
  console.log("File comment focus browser smoke passed.");
} finally {
  await browser.close();
}
