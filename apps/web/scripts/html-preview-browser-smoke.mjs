#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import { chromium } from "@playwright/test";

const [shellHtml, shellScript] = await Promise.all([
  readFile(new URL("../public/html-preview-shell.html", import.meta.url), "utf8"),
  readFile(new URL("../public/html-preview-shell.js", import.meta.url), "utf8"),
]);

const strictCsp = [
  "default-src 'self'",
  "base-uri 'self'",
  "object-src 'none'",
  "frame-ancestors 'none'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "frame-src 'self' blob:",
  "connect-src 'self'",
].join("; ");
const previewCsp = [
  "default-src data: blob: http: https:",
  "base-uri 'none'",
  "object-src 'none'",
  "frame-ancestors 'self'",
  "form-action 'none'",
  "script-src 'self' data: blob: http: https: 'unsafe-inline' 'unsafe-eval'",
  "style-src data: blob: http: https: 'unsafe-inline'",
  "img-src data: blob: http: https:",
  "font-src data: blob: http: https:",
  "media-src data: blob: http: https:",
  "frame-src data: blob: http: https:",
  "connect-src data: blob: http: https: ws: wss:",
].join("; ");

const server = createServer((request, response) => {
  const pathname = new URL(request.url || "/", "http://localhost").pathname;
  if (pathname === "/html-preview-shell.html") {
    response.writeHead(200, {
      "Cache-Control": "no-store",
      "Content-Security-Policy": previewCsp,
      "Content-Type": "text/html; charset=utf-8",
      "Referrer-Policy": "no-referrer",
      "X-Frame-Options": "SAMEORIGIN",
    });
    response.end(shellHtml);
    return;
  }
  if (pathname === "/html-preview-shell.js") {
    response.writeHead(200, { "Content-Type": "text/javascript; charset=utf-8" });
    response.end(shellScript);
    return;
  }
  if (pathname === "/external-preview-script.js") {
    response.writeHead(200, { "Content-Type": "text/javascript; charset=utf-8" });
    response.end("window.__externalPreviewScript = 'loaded';");
    return;
  }
  if (pathname === "/ordinary.txt") {
    response.writeHead(200, { "Content-Type": "text/plain; charset=utf-8" });
    response.end("network-owned");
    return;
  }
  response.writeHead(200, {
    "Content-Security-Policy": strictCsp,
    "Content-Type": "text/html; charset=utf-8",
    "X-Frame-Options": "DENY",
  });
  response.end("<!doctype html><html><body><main>Strict CSP host</main></body></html>");
});

await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const address = server.address();
assert.ok(address && typeof address === "object");
const origin = `http://127.0.0.1:${address.port}`;

const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage();
  await page.goto(origin, { waitUntil: "domcontentloaded" });

  const result = await page.evaluate(async () => {
    const waitForMessage = (predicate) => new Promise((resolve, reject) => {
      const timeout = window.setTimeout(
        () => reject(new Error("preview message timed out")),
        10_000,
      );
      const onMessage = (event) => {
        if (!predicate(event.data)) return;
        window.clearTimeout(timeout);
        window.removeEventListener("message", onMessage);
        resolve(event.data);
      };
      window.addEventListener("message", onMessage);
    });

    const mountPreview = (token, html, title) => {
      const iframe = document.createElement("iframe");
      iframe.title = title;
      const onMessage = (event) => {
        if (
          event.source === iframe.contentWindow
          && event.data?.type === "manor-html-preview:shell-ready"
          && event.data?.token === token
        ) {
          iframe.contentWindow.postMessage({
            type: "manor-html-preview:render",
            token,
            html,
          }, window.location.origin);
        }
      };
      window.addEventListener("message", onMessage);
      iframe.addEventListener("load", () => {
        window.setTimeout(() => window.removeEventListener("message", onMessage), 10_000);
      }, { once: true });
      iframe.src = `/html-preview-shell.html?preview=${encodeURIComponent(token)}`;
      document.body.append(iframe);
      return iframe;
    };

    const primaryReady = waitForMessage((data) => data?.type === "smoke:ready");
    const primaryFrame = mountPreview("smoke-primary", `<!doctype html><html><body>
      <p id="unicode">预览 ✓ &amp; &quot;quoted&quot;</p>
      <img id="blob-image">
      <img id="data-image" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='3' height='3'%3E%3C/svg%3E">
      <script src="/external-preview-script.js"></script>
      <script>
        let parentAccess = "allowed";
        try { void parent.document.body; } catch { parentAccess = "denied"; }
        const evalResult = eval("40 + 2");
        window.addEventListener("message", (event) => {
          if (event.data?.type === "smoke:ping") {
            parent.postMessage({ type: "smoke:pong", value: event.data.value }, "*");
          }
        });
        const image = document.getElementById("blob-image");
        image.src = URL.createObjectURL(new Blob([
          '<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"><rect width="2" height="2" fill="green"/></svg>'
        ], { type: "image/svg+xml" }));
        const finish = () => parent.postMessage({
          type: "smoke:ready",
          parentAccess,
          evalResult,
          externalScript: window.__externalPreviewScript,
          imageWidth: image.naturalWidth,
          dataImageWidth: document.getElementById("data-image").naturalWidth,
          unicode: document.getElementById("unicode").textContent,
          closingTagText: "</iframe>",
        }, "*");
        if (image.complete) finish(); else image.addEventListener("load", finish, { once: true });
      <\/script>
    </body></html>`, "primary preview");
    const primary = await primaryReady;

    const pongPromise = waitForMessage((data) => data?.type === "smoke:pong");
    primaryFrame.contentWindow?.postMessage({ type: "smoke:ping", value: 7 }, "*");
    const pong = await pongPromise;

    const concurrentReady = Promise.all([
      waitForMessage((data) => data?.type === "smoke:concurrent" && data.id === "a"),
      waitForMessage((data) => data?.type === "smoke:concurrent" && data.id === "b"),
    ]);
    mountPreview(
      "smoke-a",
      '<script>parent.postMessage({type:"smoke:concurrent",id:"a"},"*")<\/script>',
      "concurrent a",
    );
    mountPreview(
      "smoke-b",
      '<script>parent.postMessage({type:"smoke:concurrent",id:"b"},"*")<\/script>',
      "concurrent b",
    );
    const concurrent = await concurrentReady;

    const updateReady = waitForMessage((data) => data?.type === "smoke:update");
    mountPreview(
      "smoke-update-2",
      '<script>parent.postMessage({type:"smoke:update",revision:2},"*")<\/script>',
      "updated preview",
    );
    const update = await updateReady;

    const shellResponse = await fetch("/html-preview-shell.html");
    const ordinaryBody = await (await fetch("/ordinary.txt")).text();
    return {
      primary,
      pong,
      concurrentIds: concurrent.map((message) => message.id).sort(),
      updateRevision: update.revision,
      ordinaryBody,
      previewCsp: shellResponse.headers.get("content-security-policy"),
      previewFrameOption: shellResponse.headers.get("x-frame-options"),
    };
  });

  assert.deepEqual(result.primary, {
    type: "smoke:ready",
    parentAccess: "denied",
    evalResult: 42,
    externalScript: "loaded",
    imageWidth: 2,
    dataImageWidth: 3,
    unicode: '预览 ✓ & "quoted"',
    closingTagText: "</iframe>",
  });
  assert.deepEqual(result.pong, { type: "smoke:pong", value: 7 });
  assert.deepEqual(result.concurrentIds, ["a", "b"]);
  assert.equal(result.updateRevision, 2);
  assert.equal(result.ordinaryBody, "network-owned");
  assert.equal(result.previewFrameOption, "SAMEORIGIN");
  assert.match(result.previewCsp || "", /'unsafe-inline'/);
  assert.match(result.previewCsp || "", /'unsafe-eval'/);

  console.log("HTML preview browser smoke passed under strict CSP.");
} finally {
  await browser.close();
  await new Promise((resolve, reject) => server.close((error) => (
    error ? reject(error) : resolve(undefined)
  )));
}
