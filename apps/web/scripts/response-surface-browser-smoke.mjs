#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { build } from "esbuild";
import { chromium } from "@playwright/test";

const bundle = await build({
  stdin: {
    contents: 'export { normalizeResponseSurfaceBlock, responseSurfaceDocument } from "../src/lib/responseSurface.ts";',
    loader: "ts",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "iife",
  globalName: "ManorResponseSurface",
  platform: "browser",
  plugins: [{
    name: "stub-workspace-ledger-visualization",
    setup(context) {
      context.onResolve(
        { filter: /workspaceLedgerVisualization$/ },
        () => ({ path: "workspace-ledger-stub", namespace: "test-stub" }),
      );
      context.onLoad(
        { filter: /.*/, namespace: "test-stub" },
        () => ({
          contents: [
            "export const isWorkspaceLedgerOverview = () => true;",
            "export const isWorkspaceLedgerQueryVisualization = () => true;",
            "export const workspaceLedgerOverviewHtml = '';",
            "export const workspaceLedgerQueryHtml = '';",
          ].join("\n"),
          loader: "js",
        }),
      );
    },
  }],
  write: false,
  logLevel: "silent",
});

const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;

const componentBundle = await build({
  loader: { ".png": "dataurl", ".webp": "dataurl", ".svg": "dataurl" },
  stdin: {
    contents: `
      import React from "react";
      import { createRoot } from "react-dom/client";
      import InteractiveResponseSurface from "../src/components/InteractiveResponseSurface.tsx";
      let root;
      function RetryHarness({ surface, sourceMessageId, initialReceipts }) {
        const [submissionReceipts, setSubmissionReceipts] = React.useState(initialReceipts);
        React.useEffect(() => {
          window.__projectDurableReceipt = (receipt) => {
            setSubmissionReceipts([{
              ...receipt,
              durable: true,
              status: receipt.status || "pending",
            }]);
          };
          return () => {
            delete window.__projectDurableReceipt;
          };
        }, []);
        return React.createElement(InteractiveResponseSurface, {
          surface,
          sourceMessageId,
          submissionReceipts,
          onSubmit: async (receipt) => {
            window.__submittedEventIds.push(receipt.eventId);
            window.__submittedReceipts.push(receipt);
            if (window.__deferNextSubmission) {
              window.__deferNextSubmission = false;
              return new Promise((resolve, reject) => {
                window.__resolveSubmission = resolve;
                window.__rejectSubmission = reject;
              });
            }
            return window.__submissionOutcomes.shift();
          },
        });
      }
      window.mountResponseSurfaceRetryHarness = (surface, sourceMessageId, initialReceipts = []) => {
        root?.unmount();
        document.body.innerHTML = '<div id="response-surface-test-root"></div>';
        root = createRoot(document.getElementById("response-surface-test-root"));
        root.render(React.createElement(RetryHarness, {
          surface,
          sourceMessageId,
          initialReceipts,
        }));
      };
    `,
    loader: "tsx",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "iife",
  platform: "browser",
  define: {
    "import.meta.env": JSON.stringify({ DEV: false }),
  },
  outfile: "response-surface-component-test.js",
  write: false,
  logLevel: "silent",
});
const componentBundleJavascript = componentBundle.outputFiles.find(
  (output) => output.path.endsWith(".js"),
)?.text;
if (!componentBundleJavascript) throw new Error("Response surface component bundle was not emitted");
const previewShellHtml = await readFile(
  new URL("../public/html-preview-shell.html", import.meta.url),
  "utf8",
);
const previewShellJavascript = await readFile(
  new URL("../public/html-preview-shell.js", import.meta.url),
  "utf8",
);
const browser = await chromium.launch({
  headless: true,
  ...(executablePath ? { executablePath } : {}),
});

try {
  const page = await browser.newPage();
  await page.addScriptTag({ content: bundle.outputFiles[0].text });
  const result = await page.evaluate(() => {
    const nativeDomParser = window.DOMParser;
    let parseCount = 0;
    window.DOMParser = class extends nativeDomParser {
      parseFromString(...args) {
        parseCount += 1;
        return super.parseFromString(...args);
      }
    };

    const base = {
      id: "browser-policy",
      type: "surface",
      version: 1,
      title: "Browser policy",
      render: {
        kind: "sandboxed_html",
        code: {
          version: 1,
          runtime: "sandboxed_html",
          html: "<section>Browser policy</section>",
          css: ".card { color: var(--module-text); }",
          javascript: "",
        },
        data: {},
        validation: {
          policy: "response_surface.v2",
          code_hash: "a".repeat(64),
        },
      },
      display: { preferred: "inline", inline_height: 240, focusable: true },
      actions: [],
      fallback_markdown: "Browser policy",
    };
    const normalize = window.ManorResponseSurface.normalizeResponseSurfaceBlock;
    const renderDocument = window.ManorResponseSurface.responseSurfaceDocument;
    const withCode = (code) => normalize({
      ...base,
      render: {
        ...base.render,
        code: { ...base.render.code, ...code },
      },
    });
    const withLegacyCss = (css) => normalize({
      ...base,
      render: {
        ...base.render,
        code: { ...base.render.code, css },
        validation: { ...base.render.validation, policy: "response_surface.v1" },
      },
    });

    const results = {
      safe: Boolean(normalize(base)),
      escapedExample: Boolean(withCode({
        html: '<pre><code>&lt;div style="color:red"&gt;Hello&lt;/div&gt;</code></pre>',
      })),
      inlineStyle: withCode({ html: '<section style="color:red">Blocked</section>' }) === null,
      colorInput: withCode({ html: '<input type="color" value="#ff0000">' }) === null,
      ordinaryValueInput: Boolean(withCode({ html: '<input name="sku" value="#ff0000">' })),
      staticSvg: Boolean(withCode({
        html: '<svg viewBox="0 0 16 16"><path d="M2 8h12"></path></svg>',
        css: "svg { stroke:var(--module-text); }",
      })),
      svgSet: withCode({
        html: '<svg><rect><set attributeName="fill" to="#ff0000"></set></rect></svg>',
      }) === null,
      svgAnimate: withCode({
        html: '<svg><rect><animate attributeName="fill" values="red;blue"></animate></rect></svg>',
      }) === null,
      svgAnimateColor: withCode({
        html: '<svg><rect><animateColor attributeName="fill" values="red;blue"></animateColor></rect></svg>',
      }) === null,
      svgFilter: withCode({
        html: '<svg><filter id="f"><feColorMatrix values="0 0 0 0 1"></feColorMatrix></filter></svg>',
      }) === null,
      borderColor: withCode({
        html: '<table border="5" bordercolor="red"><tr><td>Alert</td></tr></table>',
      }) === null,
      reservedActivity: withCode({
        html: '<div id="surface-activity">Fake activity</div>',
      }) === null,
      escapedRoot: withCode({
        html: '</div><section>Outside root</section><div>',
      }) === null,
      embeddedImage: withCode({
        html: '<img alt="Preview" src="data:image/svg+xml,%3Csvg%3E%3C/svg%3E">',
      }) === null,
      malformedHandler: withCode({
        html: '<div title=">" onload="parent.postMessage(1, \'*\')">Blocked</div>',
      }) === null,
      scopeRootSelector: withCode({ css: ":scope { display:none; }" }) === null,
      nestedScopeRootSelector: withCode({
        css: ":where(:scope) { position:fixed; inset:0; }",
      }) === null,
      fixedOverlay: withCode({ css: ".cover { position:fixed; inset:0; }" }) === null,
      stickyOverlay: withCode({ css: ".cover { position:sticky; top:0; }" }) === null,
      stackedOverlay: withCode({ css: ".cover { z-index:2147483647; }" }) === null,
      escapedToken: withCode({ css: String.raw`:scope { --module\-text: transparent; }` }) === null,
      hostRootSelector: withCode({ css: "#surface-root { display:grid; }" }) === null,
      escapedImportant: withCode({ css: String.raw`.card { color:red !\69mportant; }` }) === null,
      escapedUrl: withCode({
        css: String.raw`.card { background-image:u\72l(data:image/svg+xml,%3Csvg%3E); }`,
      }) === null,
      rawColor: withCode({ css: ".card { background:#ff0000; color:red; }" }) === null,
      webkitStroke: withCode({ css: ".card { -webkit-text-stroke:4px red; }" }) === null,
      typedAttrStroke: withCode({
        html: '<section class="card" data-accent="#ff0000">Blocked</section>',
        css: ".card { -webkit-text-stroke:4px attr(data-accent type(<color>)); }",
      }) === null,
      listGradient: withCode({
        css: ".card { list-style-image:linear-gradient(CanvasText, AccentColor); }",
      }) === null,
      colorScheme: withCode({ css: ".card { color-scheme:dark; }" }) === null,
      arbitraryStrokeVariable: withCode({
        css: ".card { -webkit-text-stroke:4px var(--accent); }",
      }) === null,
      animationName: Boolean(withCode({ css: ".card { animation-name:red; }" })),
      gridArea: Boolean(withCode({ css: ".card { grid-area:red; }" })),
      contentText: Boolean(withCode({ css: '.card::before { content:"red"; }' })),
      fontFamilyText: Boolean(withCode({ css: '.card { font-family:"Arial Black", sans-serif; }' })),
      unquotedFontFamilyText: Boolean(withCode({ css: ".card { font-family:Arial Black, sans-serif; }" })),
      legacyLocalVariable: Boolean(withLegacyCss(
        ".card { --card-accent:red; color:var(--card-accent); }",
      )),
      legacyKeyframes: Boolean(withLegacyCss(
        "@keyframes pulse { from { opacity:0; } to { opacity:1; } } .card { animation:pulse 1s; }",
      )),
      legacySupports: Boolean(withLegacyCss(
        "@supports (display:grid) { .card { display:grid; } }",
      )),
      legacyHostRootSelector: Boolean(withLegacyCss("#surface-root { display:grid; }")),
      v2DocumentBlocksImages: renderDocument(base, "white").includes("img-src 'none'"),
      v1DocumentKeepsLegacyDataImages: renderDocument({
        ...base,
        render: {
          ...base.render,
          validation: { ...base.render.validation, policy: "response_surface.v1" },
        },
      }, "white").includes("img-src data:"),
    };
    window.DOMParser = nativeDomParser;
    return { ...results, parseCount };
  });

  assert.deepEqual(result, {
    safe: true,
    escapedExample: true,
    inlineStyle: true,
    colorInput: true,
    ordinaryValueInput: true,
    staticSvg: true,
    svgSet: true,
    svgAnimate: true,
    svgAnimateColor: true,
    svgFilter: true,
    borderColor: true,
    reservedActivity: true,
    escapedRoot: true,
    embeddedImage: true,
    malformedHandler: true,
    scopeRootSelector: true,
    nestedScopeRootSelector: true,
    fixedOverlay: true,
    stickyOverlay: true,
    stackedOverlay: true,
    escapedToken: true,
    hostRootSelector: true,
    escapedImportant: true,
    escapedUrl: true,
    rawColor: true,
    webkitStroke: true,
    typedAttrStroke: true,
    listGradient: true,
    colorScheme: true,
    arbitraryStrokeVariable: true,
    animationName: true,
    gridArea: true,
    contentText: true,
    fontFamilyText: true,
    unquotedFontFamilyText: true,
    legacyLocalVariable: true,
    legacyKeyframes: true,
    legacySupports: true,
    legacyHostRootSelector: true,
    v2DocumentBlocksImages: true,
    v1DocumentKeepsLegacyDataImages: true,
    parseCount: 39,
  });

  const documents = await page.evaluate(() => {
    const normalize = window.ManorResponseSurface.normalizeResponseSurfaceBlock;
    const renderDocument = window.ManorResponseSurface.responseSurfaceDocument;
    const surface = {
      id: "renderer-containment",
      type: "surface",
      version: 1,
      title: "Renderer containment",
      render: {
        kind: "sandboxed_html",
        code: {
          version: 1,
          runtime: "sandboxed_html",
          html: [
            '</div><section id="outside-root">Outside root</section><div>',
            '<div id="surface-activity">Fake activity</div>',
            '<div id="surface-error">Fake error</div>',
            '<div id="surface-root">Fake root</div>',
          ].join(""),
          css: "",
          javascript: "",
        },
        data: {},
        validation: { policy: "response_surface.v2", code_hash: "a".repeat(64) },
      },
      display: { preferred: "inline", inline_height: 240, focusable: true },
      actions: [],
      fallback_markdown: "Renderer containment",
    };
    const containment = window.ManorResponseSurface.responseSurfaceDocument(surface, "white");
    const actionSurface = {
      ...surface,
      id: "renderer-action",
      title: "Renderer action",
      render: {
        ...surface.render,
        code: {
          ...surface.render.code,
          html: '<form data-manor-action="go"><input name="answer" value="yes"><button type="submit" data-manor-action="go">Go</button></form>',
        },
      },
      actions: [{ id: "go", label: "Go" }],
    };
    const action = window.ManorResponseSurface.responseSurfaceDocument(actionSurface, "white");
    const localizedSurface = {
      ...surface,
      id: "localized-code-lab",
      title: "Localized code lab",
      render: {
        kind: "template",
        template_id: "learning.code_lab",
        template_version: 1,
        props: {
          language: "python",
          instructions: "实现 add(a, b)",
          starter_code: "def add(a, b):\n  return a + b",
          tests: ["add(2, 3) == 5"],
        },
      },
      actions: [{ id: "run", label: "运行代码" }],
    };
    const canonicalCodeLabSurface = normalize({
      ...localizedSurface,
      id: "canonical-code-lab",
      actions: [{ id: "execute", label: "Execute", intent: "submit" }],
      fallback_markdown: "Implement add.",
    });
    if (!canonicalCodeLabSurface) throw new Error("code lab action normalization failed");
    const canonicalCodeLab = renderDocument(canonicalCodeLabSurface, "white");
    const legacyStyled = renderDocument({
      ...surface,
      id: "legacy-styled",
      render: {
        ...surface.render,
        code: {
          ...surface.render.code,
          html: [
            '<section class="legacy-child">Legacy content</section>',
            '<section class="legacy-attribute">Attribute selector</section>',
            '<section class="legacy-escaped">Escaped selector</section>',
            '<section class="legacy-escaped-attribute-value">Escaped attribute value</section>',
            '<section class="legacy-escaped-attribute-name">Escaped attribute name</section>',
            '<section class="legacy-case-insensitive-attribute">Case-insensitive attribute</section>',
          ].join(""),
          css: [
            "#surface-root { display:grid; }",
            "#surface-root .legacy-child { display:none; }",
            '[id="surface-root"] .legacy-attribute { display:none; }',
            String.raw`#surface\2d root .legacy-escaped { display:none; }`,
            String.raw`[id="surface\2d root"] .legacy-escaped-attribute-value { display:none; }`,
            String.raw`[i\64="surface-root"] .legacy-escaped-attribute-name { display:none; }`,
            '[id="SURFACE-ROOT" i] .legacy-case-insensitive-attribute { display:none; }',
            '.legacy-child::before { content:"#surface-root"; }',
          ].join("\n"),
        },
        validation: { ...surface.render.validation, policy: "response_surface.v1" },
      },
    }, "white");
    const localized = window.ManorResponseSurface.responseSurfaceDocument(
      localizedSurface,
      "white",
      {},
      [{
        version: 1,
        eventId: "localized-event",
        recordedAt: "2026-08-28T00:00:00.000Z",
        sourceMessageId: "localized-message",
        surfaceId: localizedSurface.id,
        title: localizedSurface.title,
        action: "run",
        actionLabel: "Run code",
        payload: { language: "python", code: "print('ok')" },
        context: { templateId: "learning.code_lab" },
      }],
      {
        history: "交互记录",
        submitted: "已提交",
        lines: "行",
        codeExercise: "编程练习",
        codeEditor: "代码编辑器",
        language: "语言",
        line: "行",
        column: "列",
        spaces: "空格",
        checks: "检查",
        runCode: "运行代码",
        submitAnswer: "提交答案",
      },
      "zh",
    );
    return { containment, action, canonicalCodeLab, legacyStyled, localized };
  });
  await page.setContent(documents.containment);
  assert.equal(
    await page.$eval("#outside-root", (element) => element.closest("#surface-root")?.id),
    "surface-root",
    "the renderer must contain generated markup even when normalization is bypassed",
  );
  assert.deepEqual(
    await page.evaluate(() => ({
      generatedHostIds: document.querySelectorAll(
        "#surface-root #surface-activity, #surface-root #surface-error, #surface-root #surface-root",
      ).length,
      error: document.querySelectorAll("#surface-error").length,
      root: document.querySelectorAll("#surface-root").length,
    })),
    { generatedHostIds: 0, error: 1, root: 1 },
    "the renderer must strip generated host IDs even when normalization is bypassed",
  );

  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.setContent('<iframe id="surface-frame" sandbox="allow-scripts"></iframe>');
  await page.evaluate((surfaceDocument) => {
    window.__surfaceSubmission = null;
    window.__surfaceBridgeReady = null;
    const frame = document.getElementById("surface-frame");
    window.addEventListener("message", (event) => {
      if (event.data?.type === "manor:response-surface:bridge-ready") {
        window.__surfaceBridgeReady = {
          sourceMatches: event.source === frame.contentWindow,
          bridgeNonce: event.data.bridgeNonce,
        };
      }
      if (event.data?.type === "manor:response-surface:submit") {
        window.__surfaceSubmission = {
          ...event.data,
          sourceMatches: event.source === frame.contentWindow,
        };
      }
    });
    frame.srcdoc = surfaceDocument;
  }, documents.action);
  await page.waitForFunction(() => window.__surfaceBridgeReady !== null);
  await page.frameLocator("#surface-frame").locator('button[data-manor-action="go"]').click();
  await page.waitForFunction(() => window.__surfaceSubmission !== null);
  assert.deepEqual(
    await page.evaluate(() => ({
      action: window.__surfaceSubmission.action,
      payload: { ...window.__surfaceSubmission.payload },
      sourceMatches: window.__surfaceSubmission.sourceMatches,
      nonceMatches:
        window.__surfaceSubmission.bridgeNonce === window.__surfaceBridgeReady.bridgeNonce,
    })),
    {
      action: "go",
      payload: { answer: "yes" },
      sourceMatches: true,
      nonceMatches: true,
    },
    "response-surface controls must submit through the isolated iframe bridge",
  );
  assert.deepEqual(pageErrors, [], "response-surface actions must not throw browser errors");

  await page.setContent(documents.canonicalCodeLab);
  await page.evaluate(() => {
    window.__surfaceSubmission = null;
    window.addEventListener("message", (event) => {
      if (event.data?.type === "manor:response-surface:submit") {
        window.__surfaceSubmission = event.data;
      }
    });
  });
  await page.click('button[data-manor-action="run"]');
  await page.waitForFunction(() => window.__surfaceSubmission !== null);
  assert.equal(
    await page.evaluate(() => window.__surfaceSubmission.action),
    "run",
    "registered templates must canonicalize custom action ids to their rendered action",
  );

  await page.setContent(documents.legacyStyled);
  assert.deepEqual(
    await page.evaluate(() => ({
      rootDisplay: getComputedStyle(document.querySelector("#surface-root")).display,
      childDisplay: getComputedStyle(document.querySelector(".legacy-child")).display,
      attributeDisplay: getComputedStyle(document.querySelector(".legacy-attribute")).display,
      escapedDisplay: getComputedStyle(document.querySelector(".legacy-escaped")).display,
      escapedAttributeValueDisplay: getComputedStyle(
        document.querySelector(".legacy-escaped-attribute-value"),
      ).display,
      escapedAttributeNameDisplay: getComputedStyle(
        document.querySelector(".legacy-escaped-attribute-name"),
      ).display,
      caseInsensitiveAttributeDisplay: getComputedStyle(
        document.querySelector(".legacy-case-insensitive-attribute"),
      ).display,
      childContent: getComputedStyle(document.querySelector(".legacy-child"), "::before").content,
    })),
    {
      rootDisplay: "grid",
      childDisplay: "none",
      attributeDisplay: "none",
      escapedDisplay: "none",
      escapedAttributeValueDisplay: "none",
      escapedAttributeNameDisplay: "none",
      caseInsensitiveAttributeDisplay: "none",
      childContent: '"#surface-root"',
    },
    "legacy root-prefixed selectors must style the scoped root without rewriting string content",
  );

  await page.setContent(documents.localized);
  const localizedText = await page.locator("body").innerText();
  assert.match(localizedText, /编程练习/);
  assert.match(localizedText, /语言/);
  assert.match(localizedText, /检查/);
  assert.match(localizedText, /运行代码/);
  assert.doesNotMatch(localizedText, /Code exercise|Checks|Run code/);
  assert.equal(await page.locator("html").getAttribute("lang"), "zh");
  assert.equal(await page.locator("#surface-activity li strong").innerText(), "运行代码");

  await page.route("http://response-surface.test/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === "/html-preview-shell.html") {
      await route.fulfill({ contentType: "text/html", body: previewShellHtml });
      return;
    }
    if (pathname === "/html-preview-shell.js") {
      await route.fulfill({ contentType: "text/javascript", body: previewShellJavascript });
      return;
    }
    await route.fulfill({
      contentType: "text/html",
      body: '<!doctype html><html><body><div id="response-surface-test-root"></div></body></html>',
    });
  });
  await page.goto("http://response-surface.test/harness");
  await page.addScriptTag({ content: componentBundleJavascript });
  const retrySurface = {
    id: "component-retry",
    type: "surface",
    version: 1,
    title: "Retry contract",
    render: {
      kind: "sandboxed_html",
      code: {
        version: 1,
        runtime: "sandboxed_html",
        html: '<form data-manor-action="go"><input name="answer" value="yes"><button type="submit" data-manor-action="go">Go</button></form>',
        css: "form { display:grid; gap:8px; }",
        javascript: "",
      },
      data: {},
      validation: { policy: "response_surface.v2", code_hash: "a".repeat(64) },
    },
    display: { preferred: "inline", inline_height: 240, focusable: true },
    actions: [{ id: "go", label: "Go", intent: "submit" }],
    fallback_markdown: "Retry contract",
  };
  const clickHarnessAction = async () => {
    await page
      .frameLocator("iframe.response-surface-frame")
      .frameLocator("#manor-generated-preview")
      .locator('button[data-manor-action="go"]')
      .click();
  };
  const waitForHarnessActivity = async () => {
    await page
      .frameLocator("iframe.response-surface-frame")
      .frameLocator("#manor-generated-preview")
      .locator("#surface-activity")
      .waitFor();
  };

  await page.evaluate((surface) => {
    window.__submittedEventIds = [];
    window.__submittedReceipts = [];
    window.__submissionOutcomes = [
      { status: "failed", serverAccepted: false, terminalObserved: false },
      { status: "succeeded", serverAccepted: true, terminalObserved: true },
    ];
    window.mountResponseSurfaceRetryHarness(surface, "component-source-unknown");
  }, retrySurface);
  await clickHarnessAction();
  await page.waitForFunction(() => window.__submittedEventIds.length === 1);
  await clickHarnessAction();
  await page.waitForFunction(() => window.__submittedEventIds.length === 2);
  const unknownDeliveryIds = await page.evaluate(() => [...window.__submittedEventIds]);
  assert.equal(
    unknownDeliveryIds[0],
    unknownDeliveryIds[1],
    "unknown delivery failures must retry the same idempotency event",
  );

  await page.evaluate((surface) => {
    window.__submittedEventIds = [];
    window.__submittedReceipts = [];
    window.__submissionOutcomes = [
      { status: "failed", serverAccepted: true, terminalObserved: false },
      { status: "succeeded", serverAccepted: true, terminalObserved: true },
    ];
    window.mountResponseSurfaceRetryHarness(surface, "component-source-persistence-only");
  }, retrySurface);
  await clickHarnessAction();
  await page.waitForFunction(() => window.__submittedEventIds.length === 1);
  await clickHarnessAction();
  await page.waitForFunction(() => window.__submittedEventIds.length === 2);
  const persistenceOnlyFailureIds = await page.evaluate(() => [...window.__submittedEventIds]);
  assert.equal(
    persistenceOnlyFailureIds[0],
    persistenceOnlyFailureIds[1],
    "an accepted assistant-persistence failure must replay the same event",
  );

  await page.evaluate((surface) => {
    window.__submittedEventIds = [];
    window.__submittedReceipts = [];
    window.__submissionOutcomes = [
      { status: "failed", serverAccepted: true, terminalObserved: true },
      { status: "succeeded", serverAccepted: true, terminalObserved: true },
    ];
    window.mountResponseSurfaceRetryHarness(surface, "component-source-persisted");
  }, retrySurface);
  await clickHarnessAction();
  await page.waitForFunction(() => window.__submittedEventIds.length === 1);
  await clickHarnessAction();
  await page.waitForFunction(() => window.__submittedEventIds.length === 2);
  const persistedFailureIds = await page.evaluate(() => [...window.__submittedEventIds]);
  assert.notEqual(
    persistedFailureIds[0],
    persistedFailureIds[1],
    "persisted terminal failures must create a fresh event on retry",
  );

  await page.evaluate((surface) => {
    window.__submittedEventIds = [];
    window.__submittedReceipts = [];
    window.__submissionOutcomes = [
      { status: "failed", serverAccepted: false, terminalObserved: false },
      { status: "succeeded", serverAccepted: true, terminalObserved: true },
    ];
    window.mountResponseSurfaceRetryHarness(surface, "component-source-delayed-receipt");
  }, retrySurface);
  await clickHarnessAction();
  await page.waitForFunction(() => window.__submittedEventIds.length === 1);
  await page.waitForSelector(".response-surface-submit-error");
  await page.evaluate(() => {
    window.__projectDurableReceipt(window.__submittedReceipts[0]);
  });
  await page.waitForSelector(".response-surface-submit-error", { state: "detached" });
  await clickHarnessAction();
  await page.waitForFunction(() => window.__submittedEventIds.length === 2);
  const delayedReceiptIds = await page.evaluate(() => [...window.__submittedEventIds]);
  assert.equal(
    delayedReceiptIds[0],
    delayedReceiptIds[1],
    "a delayed durable receipt without an assistant outcome must reconcile the same event",
  );

  const pendingReceiptBeforeReload = await page.evaluate(() => window.__submittedReceipts[0]);
  await page.evaluate(({ surface, receipt }) => {
    window.__submittedEventIds = [];
    window.__submittedReceipts = [];
    window.__submissionOutcomes = [
      { status: "succeeded", serverAccepted: true, terminalObserved: true },
    ];
    window.mountResponseSurfaceRetryHarness(
      surface,
      "component-source-delayed-receipt",
      [{ ...receipt, durable: true, status: "pending" }],
    );
  }, { surface: retrySurface, receipt: pendingReceiptBeforeReload });
  await waitForHarnessActivity();
  await clickHarnessAction();
  await page.waitForFunction(() => window.__submittedEventIds.length === 1);
  assert.equal(
    await page.evaluate(() => window.__submittedEventIds[0]),
    pendingReceiptBeforeReload.eventId,
    "a refreshed component must retry the same accepted pending event",
  );

  await page.evaluate((surface) => {
    window.__submittedEventIds = [];
    window.__submittedReceipts = [];
    window.__submissionOutcomes = [
      { status: "succeeded", serverAccepted: false, terminalObserved: false },
      { status: "succeeded", serverAccepted: true, terminalObserved: true },
    ];
    window.mountResponseSurfaceRetryHarness(surface, "component-source-unaccepted-success");
  }, retrySurface);
  await clickHarnessAction();
  await page.waitForFunction(() => window.__submittedEventIds.length === 1);
  await page.waitForSelector(".response-surface-submit-error");
  await clickHarnessAction();
  await page.waitForFunction(() => window.__submittedEventIds.length === 2);
  const unacceptedSuccessIds = await page.evaluate(() => [...window.__submittedEventIds]);
  assert.equal(
    unacceptedSuccessIds[0],
    unacceptedSuccessIds[1],
    "a successful render without durable acceptance must retry the same event",
  );

  await page.evaluate((surface) => {
    window.__submittedEventIds = [];
    window.__submittedReceipts = [];
    window.__submissionOutcomes = [{
      status: "succeeded",
      serverAccepted: true,
      terminalObserved: true,
    }];
    window.__deferNextSubmission = true;
    window.mountResponseSurfaceRetryHarness(surface, "component-source-inflight-receipt");
  }, retrySurface);
  await clickHarnessAction();
  await page.waitForFunction(() => (
    window.__submittedReceipts.length === 1
    && typeof window.__resolveSubmission === "function"
  ));
  await page.evaluate(() => {
    window.__projectDurableReceipt(window.__submittedReceipts[0]);
  });
  await waitForHarnessActivity();
  await page.evaluate(() => {
    window.__resolveSubmission({
      status: "failed",
      serverAccepted: false,
      terminalObserved: false,
    });
  });
  await page.waitForFunction(() => (
    document.querySelector(".response-surface-runtime")?.getAttribute("aria-busy") === "false"
  ));
  assert.equal(
    await page.locator(".response-surface-submit-error").count(),
    0,
    "a durable receipt that arrives in flight must win over a later ambiguous failure",
  );
  await clickHarnessAction();
  await page.waitForFunction(() => window.__submittedEventIds.length === 2);
  const inflightReceiptIds = await page.evaluate(() => [...window.__submittedEventIds]);
  assert.equal(
    inflightReceiptIds[0],
    inflightReceiptIds[1],
    "an in-flight durable receipt without a terminal outcome must replay the same event",
  );

  await page.evaluate((surface) => {
    window.__submittedEventIds = [];
    window.__submittedReceipts = [];
    window.__submissionOutcomes = [{
      status: "succeeded",
      serverAccepted: true,
      terminalObserved: true,
    }];
    window.__deferNextSubmission = true;
    window.mountResponseSurfaceRetryHarness(surface, "component-source-accepted-failure");
  }, retrySurface);
  await clickHarnessAction();
  await page.waitForFunction(() => (
    window.__submittedReceipts.length === 1
    && typeof window.__resolveSubmission === "function"
  ));
  await page.evaluate(() => {
    window.__projectDurableReceipt(window.__submittedReceipts[0]);
  });
  await waitForHarnessActivity();
  await page.evaluate(() => {
    window.__resolveSubmission({
      status: "failed",
      serverAccepted: true,
      terminalObserved: true,
    });
  });
  await page.waitForFunction(() => (
    document.querySelector(".response-surface-runtime")?.getAttribute("aria-busy") === "false"
  ));
  assert.equal(
    await page.locator(".response-surface-submit-error").count(),
    1,
    "a durable receipt must not hide a determinate accepted failure",
  );
  await clickHarnessAction();
  await page.waitForFunction(() => window.__submittedEventIds.length === 2);
  const acceptedFailureIds = await page.evaluate(() => [...window.__submittedEventIds]);
  assert.notEqual(
    acceptedFailureIds[0],
    acceptedFailureIds[1],
    "a determinate accepted failure must use a new event on retry",
  );

  await page.evaluate((surface) => {
    window.__submittedEventIds = [];
    window.__submittedReceipts = [];
    window.__submissionOutcomes = [{
      status: "succeeded",
      serverAccepted: true,
      terminalObserved: true,
    }];
    window.__deferNextSubmission = true;
    window.mountResponseSurfaceRetryHarness(surface, "component-source-inflight-throw");
  }, retrySurface);
  await clickHarnessAction();
  await page.waitForFunction(() => (
    window.__submittedReceipts.length === 1
    && typeof window.__rejectSubmission === "function"
  ));
  await page.evaluate(() => {
    window.__projectDurableReceipt(window.__submittedReceipts[0]);
  });
  await waitForHarnessActivity();
  await page.evaluate(() => {
    window.__rejectSubmission(new Error("transport disconnected"));
  });
  await page.waitForFunction(() => (
    document.querySelector(".response-surface-runtime")?.getAttribute("aria-busy") === "false"
  ));
  assert.equal(
    await page.locator(".response-surface-submit-error").count(),
    0,
    "a durable receipt must also win when the in-flight request rejects",
  );
  await clickHarnessAction();
  await page.waitForFunction(() => window.__submittedEventIds.length === 2);
  const inflightThrowIds = await page.evaluate(() => [...window.__submittedEventIds]);
  assert.equal(
    inflightThrowIds[0],
    inflightThrowIds[1],
    "a rejected request with acceptance but no terminal outcome must reuse its event",
  );
  console.log("Response surface browser policy smoke passed.");
} finally {
  await browser.close();
}
