import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { build } from "esbuild";
import {
  cloneBoundedResponseSurfacePayload,
  latestUnseenResponseSurfaceReceipt,
  responseSurfaceSubmissionFailureMessageIds,
  responseSurfaceSubmissionOutcomes,
  RESPONSE_SURFACE_CODE_LAB_PAYLOAD_LIMIT,
  RESPONSE_SURFACE_DRAFT_PAYLOAD_LIMIT,
  RESPONSE_SURFACE_GENERIC_PAYLOAD_LIMIT,
} from "../src/lib/responseSurfaceState.mjs";

test("response surface failures stay out of chat while lifecycle state remains durable", () => {
  const receiptIds = new Set([
    "receipt-1",
    "receipt-credit",
    "receipt-error",
    "receipt-pending",
    "receipt-success",
  ]);
  const messages = [
    {
      id: "failed-assistant",
      role: "assistant",
      meta: {
        origin_user_message_id: "receipt-1",
        stream_status: "error",
      },
    },
    {
      id: "successful-assistant",
      role: "assistant",
      meta: {
        origin_user_message_id: "receipt-1",
        stream_status: "completed",
      },
    },
    {
      id: "ordinary-error",
      role: "assistant",
      meta: {
        origin_user_message_id: "ordinary-user",
        stream_status: "error",
      },
    },
    {
      id: "credit-assistant",
      role: "assistant",
      meta: {
        origin_user_message_id: "receipt-credit",
        stop_reason: "credit_exhausted",
        error: "No credits",
      },
    },
    {
      id: "persisted-error-assistant",
      role: "assistant",
      meta: {
        origin_user_message_id: "receipt-error",
        stop_reason: "error",
      },
    },
    {
      id: "pending-assistant",
      role: "assistant",
      meta: {
        origin_user_message_id: "receipt-pending",
        stream_status: "running",
      },
    },
    {
      id: "completed-assistant",
      role: "assistant",
      meta: {
        origin_user_message_id: "receipt-success",
      },
    },
  ];
  const hidden = responseSurfaceSubmissionFailureMessageIds(messages, receiptIds);
  const outcomes = responseSurfaceSubmissionOutcomes(messages, receiptIds);

  assert.deepEqual(
    [...hidden],
    ["failed-assistant", "credit-assistant", "persisted-error-assistant"],
  );
  assert.equal(outcomes.outcomesByReceiptMessageId.get("receipt-1"), "failed");
  assert.equal(outcomes.outcomesByReceiptMessageId.get("receipt-credit"), "failed");
  assert.equal(outcomes.outcomesByReceiptMessageId.get("receipt-error"), "failed");
  assert.equal(outcomes.statusesByReceiptMessageId.get("receipt-pending"), "pending");
  assert.equal(outcomes.statusesByReceiptMessageId.get("receipt-success"), "succeeded");
  assert.equal(outcomes.statusesByReceiptMessageId.get("receipt-1"), "failed");
});

async function source(path) {
  return readFile(new URL(`../${path}`, import.meta.url), "utf8");
}

test("response surface state preserves retries and supported code lab sizes", () => {
  const prior = { eventId: "prior", payload: { code: "old" } };
  const current = { eventId: "current", payload: { code: "new" } };
  const seen = new Set();

  assert.equal(latestUnseenResponseSurfaceReceipt([prior], seen), prior);
  assert.equal(latestUnseenResponseSurfaceReceipt([prior, current], seen), current);
  assert.equal(
    latestUnseenResponseSurfaceReceipt([prior], seen),
    null,
    "rolling back an optimistic receipt must not rehydrate the older payload",
  );

  const escapedCodePayload = {
    language: "python",
    code: "\\".repeat(30_000),
  };
  assert.equal(
    cloneBoundedResponseSurfacePayload(
      escapedCodePayload,
      RESPONSE_SURFACE_GENERIC_PAYLOAD_LIMIT,
    ),
    null,
  );
  assert.deepEqual(
    cloneBoundedResponseSurfacePayload(
      escapedCodePayload,
      RESPONSE_SURFACE_CODE_LAB_PAYLOAD_LIMIT,
    ),
    escapedCodePayload,
  );

  const code = "x".repeat(30_000);
  const draftsByLanguage = Object.fromEntries(
    ["python", "javascript", "typescript", "java", "cpp", "go", "rust", "ruby"]
      .map((language) => [language, code]),
  );
  const fullDraft = { language: "python", code, draftsByLanguage };
  assert.ok(JSON.stringify(fullDraft).length > 250_000);
  assert.deepEqual(
    cloneBoundedResponseSurfacePayload(fullDraft, RESPONSE_SURFACE_DRAFT_PAYLOAD_LIMIT),
    fullDraft,
  );
});

test("assistant response surfaces use one validated rendering path", async () => {
  const [blocks, renderer, runtime, shell, shellHtml] = await Promise.all([
    source("src/components/AssistantMessageBlocks.tsx"),
    source("src/components/InteractiveResponseSurface.tsx"),
    source("src/lib/responseSurface.ts"),
    source("public/html-preview-shell.js"),
    source("public/html-preview-shell.html"),
  ]);

  assert.match(blocks, /normalizeResponseSurfaceBlock/);
  assert.match(blocks, /<InteractiveResponseSurface/);
  assert.match(blocks, /workspace\.ledger\.overview/);
  assert.match(blocks, /workspace\.ledger\.query/);

  assert.match(renderer, /useIsolatedHtmlPreview/);
  assert.match(renderer, /event\.source !== iframeRef\.current\?\.contentWindow/);
  assert.match(renderer, /message\.surfaceId !== surface\.id/);
  assert.match(renderer, /bridgeNonceRef/);
  assert.match(renderer, /message\.bridgeNonce/);
  assert.match(renderer, /response-surface-revision:\$\{documentRevision\}/);
  assert.match(renderer, /requestViewTransition/);
  assert.match(renderer, /manor:response-surface:request-state/);
  assert.match(renderer, /pending\?\.requestId === requestId/);
  assert.match(renderer, /lateViewStateRef/);
  assert.match(renderer, /draftVersionRef/);
  assert.match(renderer, /RESPONSE_SURFACE_DRAFT_PAYLOAD_LIMIT/);
  assert.match(renderer, /latestUnseenResponseSurfaceReceipt/);
  assert.match(renderer, /\.\.\.draftRef\.current/);
  assert.match(renderer, /event\.source === late\.sourceWindow/);
  assert.match(renderer, /finishViewTransition\(false\)/);
  assert.match(renderer, /await onSubmit/);
  assert.match(renderer, /pendingSubmissionReceiptRef/);
  assert.match(renderer, /durablePendingReceiptRef/);
  assert.match(renderer, /createResponseSurfaceSubmissionReceipt/);
  assert.match(renderer, /responseSurfaceSubmissionIntentKey/);
  assert.match(renderer, /pending\?\.intentKey === intentKey/);
  assert.match(renderer, /onSubmit\(receipt\)/);
  assert.match(renderer, /response-surface-submit-error/);
  assert.match(renderer, /interactionReadOnly/);
  assert.match(renderer, /sourceMessageId/);
  assert.match(renderer, /submissionReceipts/);
  assert.match(renderer, /getLocale/);
  assert.doesNotMatch(renderer, /\)\)\.slice\(-20\), \[sourceMessageId/);
  assert.match(renderer, /<Modal/);
  assert.match(renderer, /onResponseSurfaceSubmit|onSubmit/);

  assert.match(runtime, /learning\.code_lab/);
  assert.match(runtime, /response\.choice/);
  assert.match(runtime, /RESPONSE_SURFACE_CODE_LAB_PAYLOAD_LIMIT/);
  assert.match(runtime, /Content-Security-Policy/);
  assert.match(runtime, /connect-src 'none'/);
  assert.match(runtime, /form-action 'none'/);
  assert.match(runtime, /bridgeNonce/);
  assert.match(runtime, /crypto\.randomUUID/);
  assert.match(runtime, /event\.isTrusted/);
  assert.match(runtime, /event\.data\?\.bridgeNonce !== bridgeNonce/);
  assert.match(runtime, /manor-html-preview:host-ready/);
  assert.match(runtime, /manor:response-surface:request-state/);
  assert.match(runtime, /field\.checked = selected\.has\(field\.value\)/);
  assert.match(runtime, /field\.selectedOptions/);
  assert.match(runtime, /Object\.create/);
  assert.match(runtime, /Node\.prototype\.contains/);
  assert.match(runtime, /nodeContains\.call\(form, submitter\)/);
  assert.match(runtime, /event\.preventDefault\(\)/);
  assert.match(runtime, /checkFormValidity\.call\(form\)/);
  assert.match(runtime, /reportFormValidity\.call\(form\)/);
  const submitHandler = runtime.slice(
    runtime.indexOf('document.addEventListener("submit"'),
    runtime.indexOf('document.addEventListener("click"'),
  );
  assert.match(submitHandler, /event\.target instanceof Form/);
  assert.match(submitHandler, /checkFormValidity\.call\(form\)/);
  assert.match(submitHandler, /reportFormValidity\.call\(form\)/);
  assert.match(runtime, /HTMLButtonElement/);
  assert.match(runtime, /submitter instanceof Button && submitter\.type === "submit"/);
  assert.match(runtime, /manor:response-surface:submit/);
  assert.match(runtime, /responseSurfaceSubmissionMessage/);
  assert.match(runtime, /createResponseSurfaceSubmissionReceipt/);
  assert.match(runtime, /collectResponseSurfaceSubmissionReceipts/);
  assert.match(runtime, /rollbackResponseSurfaceSubmissionMessages/);
  assert.match(runtime, /response_surface_submission_pending === true/);
  assert.match(runtime, /legacySubmissionPayload/);
  assert.match(runtime, /isUserAuthoredResponseSurfaceMessage/);
  assert.match(runtime, /message\.role, message\.author_kind/);
  assert.match(runtime, /if \(!isUserAuthoredResponseSurfaceMessage\(message\)\) continue/);
  assert.match(runtime, /legacy:\$\{messageId\}/);
  assert.match(runtime, /surfacesBySignature/);
  assert.match(runtime, /surface-activity/);
  assert.match(runtime, /const rows = receipts\.slice\(-20\)\.reverse\(\)/);
  assert.match(runtime, /SURFACE_GENERATED_POLICY_CSS/);
  assert.match(runtime, /@scope \(#surface-root\)/);
  assert.match(runtime, /rewriteLegacySurfaceRootSelectors/);
  assert.match(runtime, /REGISTERED_TEMPLATE_ACTIONS/);
  assert.match(runtime, /generatedCssAllowed/);
  assert.match(runtime, /generatedHtmlAllowed/);
  assert.match(runtime, /DOMParser/);
  assert.match(runtime, /BLOCKED_PRESENTATION_ATTRIBUTES/);
  assert.match(runtime, /const reservedHostIds = new Set/);
  assert.match(runtime, /surfaceRoot\.querySelectorAll\("\[id\]"\)/);
  assert.match(runtime, /response-surface-generated/);
  assert.match(runtime, /data-manor-ui-policy="response-surface\.v1"/);
  assert.match(runtime, /generatedSurface \? SURFACE_GENERATED_POLICY_CSS : ""/);
  assert.match(runtime, /SURFACE_ACTIVITY_CSS/);
  assert.match(runtime, /const latest = rows\[0\]!/);
  assert.match(runtime, /const previous = rows\.slice\(1\)/);
  assert.match(runtime, /previous\.map\(rowMarkup\)/);
  assert.match(runtime, /DEFAULT_CODE_LAB_LANGUAGES/);
  assert.match(runtime, /data-code-language/);
  assert.match(runtime, /data-code-lines/);
  assert.match(runtime, /data-code-highlight/);
  assert.match(runtime, /data-code-position/);
  assert.match(runtime, /highlightCode/);
  assert.match(runtime, /syntax-keyword/);
  assert.match(runtime, /--ide-editor/);
  assert.match(runtime, /grid-template-columns:40px minmax\(0,1fr\)/);
  assert.match(runtime, /\? "code-activity" : "surface-activity"/);
  assert.match(runtime, /<details><summary/);
  assert.doesNotMatch(runtime, /#0d1117|#161b22/);
  assert.match(runtime, /editor\.setRangeText\("  "/);
  assert.match(runtime, /drafts\[currentLanguage\]/);
  assert.match(runtime, /draftsByLanguage/);
  assert.match(runtime, /collectResponseSurfaceState/);
  assert.match(runtime, /statePayload/);
  assert.match(runtime, /document\.addEventListener\("change"/);
  assert.match(runtime, /response_surface_submission/);
  assert.match(runtime, /event\.data\.busy === true/);
  assert.match(runtime, /surfaceRoot\.innerHTML =/);
  assert.match(runtime, /nodeContains\.call\(surfaceRoot, form\)/);
  assert.match(runtime, /nodeContains\.call\(surfaceRoot, event\.target\)/);
  assert.match(runtime, /queryAll\.call\(surfaceRoot, "input, select, textarea, button, \[data-manor-action\]"\)/);
  assert.match(runtime, /sandbox-backed bash tool/);
  assert.match(runtime, /select:bash/);
  assert.match(runtime, /even if an earlier turn reported that bash was unavailable/);
  assert.match(runtime, /Do not claim any test passed unless the tool output proves it/);
  assert.match(runtime, /Do not write files to the Workspace/);
  assert.match(runtime, /javascript\.trim\(\)/);
  assert.match(runtime, /theme, draft, receipts, labels, false/);
  assert.match(runtime, /executeBundleJavascript/);
  assert.match(renderer, /templateProps\.tests/);
  assert.match(shell, /event\.source === preview\.contentWindow/);
  assert.match(shell, /manor-html-preview:host-ready/);
  assert.match(shellHtml, /sandbox="allow-scripts"/);
  assert.doesNotMatch(shellHtml, /allow-popups|allow-modals|allow-forms/);
});

test("generated surface fallback validation preserves v1 styling", async () => {
  const bundled = await build({
    stdin: {
      contents: [
        "export {",
        "  collectResponseSurfaceSubmissionReceipts,",
        "  isResponseSurfaceSubmissionMessage,",
        "  normalizeResponseSurfaceBlock,",
        "  responseSurfaceSubmissionMeta,",
        "  settleResponseSurfaceSubmissionFailure,",
        '} from "../src/lib/responseSurface.ts";',
      ].join("\n"),
      loader: "ts",
      resolveDir: new URL(".", import.meta.url).pathname,
    },
    bundle: true,
    format: "esm",
    platform: "browser",
    plugins: [{
      name: "stub-workspace-ledger-visualization",
      setup(bundle) {
        bundle.onResolve(
          { filter: /workspaceLedgerVisualization$/ },
          () => ({ path: "workspace-ledger-stub", namespace: "test-stub" }),
        );
        bundle.onLoad(
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
  const moduleUrl = `data:text/javascript;base64,${Buffer.from(bundled.outputFiles[0].text).toString("base64")}`;
  const {
    collectResponseSurfaceSubmissionReceipts,
    isResponseSurfaceSubmissionMessage,
    normalizeResponseSurfaceBlock,
    responseSurfaceSubmissionMeta,
    settleResponseSurfaceSubmissionFailure,
  } = await import(moduleUrl);
  const legacy = {
    id: "legacy-safe-style",
    type: "surface",
    version: 1,
    title: "Legacy surface",
    render: {
      kind: "sandboxed_html",
      code: {
        version: 1,
        runtime: "sandboxed_html",
        html: '<section style="padding: 12px">Still visible</section>',
        css: "",
        javascript: "",
      },
      data: {},
      validation: {
        policy: "response_surface.v1",
        code_hash: "a".repeat(64),
      },
    },
    display: { preferred: "inline", inline_height: 240, focusable: true },
    actions: [],
    fallback_markdown: "Still visible",
  };

  assert.ok(normalizeResponseSurfaceBlock(legacy));
  assert.deepEqual(
    normalizeResponseSurfaceBlock({
      id: "canonical-template-action",
      type: "surface",
      version: 1,
      title: "Canonical template action",
      render: {
        kind: "template",
        template_id: "learning.code_lab",
        template_version: 1,
        props: {
          language: "python",
          instructions: "Implement add(a, b).",
          starter_code: "def add(a, b): pass",
          tests: ["add(2, 3) == 5"],
        },
      },
      display: { preferred: "inline", inline_height: 360, focusable: true },
      actions: [{ id: "execute", label: "Execute", intent: "submit" }],
      fallback_markdown: "Implement add.",
    })?.actions,
    [{ id: "run", label: "Run code", intent: "submit" }],
    "registered templates must repair stored custom action ids",
  );
  const historicalCodeLab = {
    id: "historical-code-lab",
    type: "surface",
    version: 1,
    title: "Old code lab",
    render: {
      kind: "template",
      template_id: "learning.code_lab",
      template_version: 1,
      props: {
        language: "python",
        instructions: "Print one.",
        starter_code: "print(1)",
        tests: ["prints 1"],
      },
    },
    display: { preferred: "inline", inline_height: 360, focusable: true },
    actions: [{ id: "execute", label: "Execute", intent: "submit" }],
    fallback_markdown: "Print one.",
  };
  const historicalSource = {
    id: "historical-assistant",
    role: "assistant",
    assistant_blocks: [historicalCodeLab],
  };
  const historicalStructuredMessage = {
    id: "historical-structured-user",
    role: "user",
    meta: {
      response_surface_submission: {
        version: 1,
        eventId: "historical-structured-event",
        recordedAt: "2026-08-25T00:00:00.000Z",
        sourceMessageId: historicalSource.id,
        surfaceId: historicalCodeLab.id,
        title: historicalCodeLab.title,
        action: "execute",
        actionLabel: "Execute",
        payload: { language: "python", code: "print(1)" },
        context: { templateId: "learning.code_lab" },
      },
    },
  };
  const [historicalStructuredReceipt] = collectResponseSurfaceSubmissionReceipts([
    historicalSource,
    historicalStructuredMessage,
    {
      id: "historical-failed-assistant",
      role: "assistant",
      meta: {
        origin_user_message_id: historicalStructuredMessage.id,
        stream_status: "error",
      },
    },
  ]);
  assert.deepEqual(
    {
      action: historicalStructuredReceipt?.action,
      actionLabel: historicalStructuredReceipt?.actionLabel,
      payload: historicalStructuredReceipt?.payload,
      outcome: historicalStructuredReceipt?.outcome,
      durable: historicalStructuredReceipt?.durable,
    },
    {
      action: "run",
      actionLabel: "Run code",
      payload: { language: "python", code: "print(1)" },
      outcome: "failed",
      durable: true,
    },
    "historical structured receipts must project onto the canonical template action",
  );
  assert.deepEqual(
    settleResponseSurfaceSubmissionFailure([
      historicalStructuredMessage,
      { role: "assistant", content: "Error: hidden", stream_error: true },
    ], "historical-structured-event", "failed"),
    [{
      ...historicalStructuredMessage,
      meta: {
        response_surface_submission: {
          ...historicalStructuredMessage.meta.response_surface_submission,
          outcome: "failed",
        },
        response_surface_submission_pending: false,
      },
    }],
    "persisted failures stay attached to the HTML receipt without a chat error bubble",
  );
  const optimisticStructuredMessage = {
    ...historicalStructuredMessage,
    id: "optimistic-structured-user",
    meta: responseSurfaceSubmissionMeta(
      historicalStructuredMessage.meta.response_surface_submission,
    ),
  };
  assert.equal(
    collectResponseSurfaceSubmissionReceipts([
      historicalSource,
      optimisticStructuredMessage,
    ])[0]?.durable,
    false,
    "optimistic receipts must not be mistaken for durable history",
  );
  const [durableWithoutAssistant] = collectResponseSurfaceSubmissionReceipts([
    historicalSource,
    {
      ...optimisticStructuredMessage,
      id: "durable-structured-user",
      meta: {
        response_surface_submission:
          historicalStructuredMessage.meta.response_surface_submission,
      },
    },
  ]);
  assert.equal(
    durableWithoutAssistant?.durable,
    true,
    "the same event becomes durable after the server message is projected",
  );
  assert.equal(
    durableWithoutAssistant?.status,
    undefined,
    "historical receipts without linked assistant lifecycle evidence must not be restored as pending",
  );
  const durableStructuredMessage = {
    ...optimisticStructuredMessage,
    id: "durable-before-optimistic-user",
    meta: {
      response_surface_submission:
        historicalStructuredMessage.meta.response_surface_submission,
    },
  };
  assert.equal(
    collectResponseSurfaceSubmissionReceipts([
      historicalSource,
      durableStructuredMessage,
      optimisticStructuredMessage,
    ])[0]?.durable,
    true,
    "a later optimistic duplicate must not overwrite durable receipt evidence",
  );
  assert.equal(
    collectResponseSurfaceSubmissionReceipts([
      historicalSource,
      optimisticStructuredMessage,
      durableStructuredMessage,
    ])[0]?.durable,
    true,
    "a later durable projection must replace its optimistic duplicate",
  );
  const historicalPlaintextMessage = {
    id: "historical-plaintext-user",
    role: "user",
    content: "Execute: Old code lab\n\n```python\nprint(1)\n```",
    created_at: "2026-08-25T00:00:01.000Z",
  };
  const historicalPlaintextReceipts = collectResponseSurfaceSubmissionReceipts([
    historicalSource,
    historicalPlaintextMessage,
  ]);
  assert.equal(historicalPlaintextReceipts[0]?.action, "run");
  assert.equal(historicalPlaintextReceipts[0]?.actionLabel, "Run code");
  assert.equal(
    historicalPlaintextReceipts[0]?.status,
    undefined,
    "legacy plaintext receipts must never be inferred as pending",
  );
  assert.equal(
    isResponseSurfaceSubmissionMessage(
      historicalPlaintextMessage,
      historicalPlaintextReceipts,
    ),
    true,
    "historical plaintext submissions must stay hidden after canonicalization",
  );
  assert.ok(normalizeResponseSurfaceBlock({
    ...legacy,
    render: {
      ...legacy.render,
      code: {
        ...legacy.render.code,
        html: '<pre><code>&lt;div style="color:red"&gt;Hello&lt;/div&gt;</code></pre>',
      },
      validation: { ...legacy.render.validation, policy: "response_surface.v2" },
    },
  }), "escaped code examples must remain visible in v2 surfaces");
  assert.equal(
    normalizeResponseSurfaceBlock({
      ...legacy,
      render: {
        ...legacy.render,
        validation: { ...legacy.render.validation, policy: "response_surface.v2" },
      },
    }),
    null,
    "new v2 surfaces must keep inline style attributes out of generated HTML",
  );
  assert.equal(
    normalizeResponseSurfaceBlock({
      ...legacy,
      render: {
        ...legacy.render,
        code: {
          ...legacy.render.code,
          html: '<svg/onload="parent.postMessage(\'escaped\', \'*\')"></svg>',
        },
      },
    }),
    null,
    "historical surfaces must reject malformed event-handler separators",
  );
  assert.equal(
    normalizeResponseSurfaceBlock({
      ...legacy,
      render: {
        ...legacy.render,
        code: {
          ...legacy.render.code,
          html: '<div title=">" onload="parent.postMessage(\'escaped\', \'*\')">Escaped</div>',
        },
      },
    }),
    null,
    "tag scanning must not stop at a greater-than sign inside a quoted attribute",
  );
  assert.equal(
    normalizeResponseSurfaceBlock({
      ...legacy,
      render: {
        ...legacy.render,
        code: {
          ...legacy.render.code,
          html: '<section/style="position:fixed;inset:0">Escaped</section>',
        },
        validation: { ...legacy.render.validation, policy: "response_surface.v2" },
      },
    }),
    null,
    "v2 surfaces must reject malformed inline-style separators",
  );
  for (const html of [
    '<svg><rect fill="#ff0000"></rect></svg>',
    '<svg><path stroke="#ff0000"></path></svg>',
    '<table bgcolor="#ff0000"><tr><td>Alert</td></tr></table>',
    '<table border="5" bordercolor="red"><tr><td>Alert</td></tr></table>',
    '<font color="#ff0000">Alert</font>',
    '<input type="color" value="#ff0000">',
    '<div id="surface-root">Fake root</div>',
    '<div id="surface-activity">Fake activity</div>',
    '<div id="surface-error">Fake error</div>',
    '</div><section>Outside root</section><div>',
    '<svg><rect><set attributeName="fill" to="#ff0000"></set></rect></svg>',
    '<svg><rect><animate attributeName="fill" values="red;blue"></animate></rect></svg>',
    '<svg><rect><animateColor attributeName="fill" values="red;blue"></animateColor></rect></svg>',
    '<svg><filter id="f"><feColorMatrix values="0 0 0 0 1"></feColorMatrix></filter></svg>',
    '<img alt="Preview" src="data:image/svg+xml,%3Csvg%3E%3C/svg%3E">',
  ]) {
    assert.equal(
      normalizeResponseSurfaceBlock({
        ...legacy,
        render: {
          ...legacy.render,
          code: { ...legacy.render.code, html },
          validation: { ...legacy.render.validation, policy: "response_surface.v2" },
        },
      }),
      null,
      `v2 surfaces must reject presentational color attributes in ${html}`,
    );
  }
  for (const css of [
    "#surface-root { display: grid; }",
    ":scope { display: none; }",
    ":where(:scope) { position: fixed; inset: 0; }",
    ".cover { position: fixed; inset: 0; }",
    ".cover { position: sticky; top: 0; }",
    ".cover { z-index: 2147483647; }",
    String.raw`:scope { --module\-text: transparent; }`,
    String.raw`.card { color: red !\69mportant; }`,
    String.raw`.card { background-image: u\72l(data:image/svg+xml,%3Csvg%3E); }`,
    ".card { background: #ff0000; }",
    ".card { color: red; }",
    ".card { color: rgb(255 0 0); }",
    ".card { -webkit-text-stroke: 4px red; }",
    String.raw`.card { -webkit-text-stroke: 4px r\65 d; }`,
    ".card { -webkit-text-stroke: 4px #ff0000; }",
    ".card { -webkit-text-stroke: 4px attr(data-accent type(<color>)); }",
    String.raw`.card { -webkit-text-stroke: 4px a\74 tr(data-accent type(<color>)); }`,
    ".card { -webkit-text-stroke: 4px var(--accent); }",
    ".card { list-style-image: linear-gradient(red, blue); }",
    ".card { list-style-image: linear-gradient(CanvasText, AccentColor); }",
    ".card { list-style-image: linear-gradient(var(--accent), var(--module-surface)); }",
    ".card { color-scheme: dark; }",
  ]) {
    assert.equal(
      normalizeResponseSurfaceBlock({
        ...legacy,
        render: {
          ...legacy.render,
          code: { ...legacy.render.code, html: "<section>Generated</section>", css },
          validation: { ...legacy.render.validation, policy: "response_surface.v2" },
        },
      }),
      null,
      `v2 surfaces must reject CSS policy bypass in ${css}`,
    );
  }
  for (const css of [
    ".card { color: var(--module-text); background: var(--module-surface); }",
    ".card { border: 1px solid var(--module-border); box-shadow: 0 4px 12px var(--module-border); }",
    ".card { -webkit-text-stroke: 1px var(--module-accent); }",
    ".card { list-style-image: linear-gradient(var(--module-accent), var(--module-surface)); }",
    ".card { transition: background 160ms ease; }",
    ".card { animation-name: red; }",
    ".card { grid-area: red; }",
    '.card::before { content: "red"; }',
    '.card { font-family: "Arial Black", sans-serif; }',
    ".card { font-family: Arial Black, sans-serif; }",
    "@media (max-width: 640px) { .card { background: var(--module-row); } }",
  ]) {
    assert.ok(normalizeResponseSurfaceBlock({
      ...legacy,
      render: {
        ...legacy.render,
        code: { ...legacy.render.code, html: "<section>Generated</section>", css },
        validation: { ...legacy.render.validation, policy: "response_surface.v2" },
      },
    }), `v2 surfaces must accept host-token CSS in ${css}`);
  }
  assert.ok(normalizeResponseSurfaceBlock({
    ...legacy,
    render: {
      ...legacy.render,
      code: { ...legacy.render.code, css: ".legacy { color: red; }" },
    },
  }), "historical v1 surfaces keep their original color styling");
  assert.ok(normalizeResponseSurfaceBlock({
    ...legacy,
    render: {
      ...legacy.render,
      code: { ...legacy.render.code, css: "#surface-root { display: grid; }" },
    },
  }), "historical v1 surfaces keep host-root selectors that were valid when stored");
  assert.ok(normalizeResponseSurfaceBlock({
    ...legacy,
    render: {
      ...legacy.render,
      code: { ...legacy.render.code, html: '<section id="surface-root">Legacy content</section>' },
    },
  }), "historical v1 surfaces keep reserved ids that the renderer safely strips");
  assert.ok(normalizeResponseSurfaceBlock({
    ...legacy,
    render: {
      ...legacy.render,
      code: {
        ...legacy.render.code,
        css: ".legacy { --card-accent: red; color: var(--card-accent); }",
      },
    },
  }), "historical v1 surfaces keep local custom properties");
  for (const css of [
    "@keyframes pulse { from { opacity: 0; } to { opacity: 1; } } .legacy { animation: pulse 1s; }",
    "@supports (display: grid) { .legacy { display: grid; } }",
  ]) {
    assert.ok(normalizeResponseSurfaceBlock({
      ...legacy,
      render: {
        ...legacy.render,
        code: { ...legacy.render.code, css },
      },
    }), `historical v1 surfaces keep supported at-rules in ${css}`);
  }
  assert.equal(normalizeResponseSurfaceBlock({
    ...legacy,
    render: {
      ...legacy.render,
      code: {
        ...legacy.render.code,
        css: String.raw`.legacy { background-image: u\72l(data:image/svg+xml,%3Csvg%3E); }`,
      },
    },
  }), null, "historical v1 surfaces cannot restore escaped resource loading");
  assert.equal(
    normalizeResponseSurfaceBlock({
      ...legacy,
      render: {
        ...legacy.render,
        validation: {
          ...legacy.render.validation,
          code_hash: `${"a".repeat(64)}suffix`,
        },
      },
    }),
    null,
    "validation hashes must be checked before any length normalization",
  );
});

test("all internal chat entrypoints return surface submissions as user turns", async () => {
  const [embedded, floating, workspace, api, retry] = await Promise.all([
    source("src/components/EmbeddedChat.tsx"),
    source("src/components/FloatingChat.tsx"),
    source("src/components/WorkspaceChat.tsx"),
    source("src/lib/api.ts"),
    source("src/lib/chatRetry.ts"),
  ]);

  for (const chat of [embedded, floating, workspace]) {
    assert.match(chat, /responseSurfaceSubmissionMessage/);
    assert.match(chat, /responseSurfaceSubmissionMeta/);
    assert.match(chat, /isResponseSurfaceSubmissionMessage/);
    assert.match(chat, /responseSurfaceSubmission:/);
    assert.match(chat, /handleResponseSurfaceSubmit/);
    assert.match(chat, /onResponseSurfaceSubmit=/);
    assert.match(chat, /sourceMessageId=/);
    assert.match(chat, /responseSurfaceSubmissionReceipts=/);
    assert.match(chat, /const isResponseSurfaceSubmission = Boolean\(/);
    assert.match(chat, /rollbackResponseSurfaceSubmissionMessages/);
    assert.match(chat, /collectResponseSurfaceSubmissionFailureMessageIds/);
    assert.match(chat, /ChatStreamCompletionStatus\.Succeeded/);
    assert.match(chat, /sendSucceeded = status === ChatStreamCompletionStatus\.Succeeded/);
    assert.match(
      chat,
      /if \(!accepted\.serverAccepted\) \{/,
      "an unaccepted response must roll back its optimistic HTML activity",
    );
    assert.match(
      chat,
      /accepted\.status !== "succeeded" && accepted\.terminalObserved/,
      "only a determinate accepted failure may settle the HTML activity as failed",
    );
    assert.doesNotMatch(chat, /createResponseSurfaceSubmissionReceipt/);
  }
  assert.match(embedded, /if \(!isResponseSurfaceSubmission\) \{[\s\S]{0,180}setInput\(""\)/);
  assert.match(workspace, /if \(!isResponseSurfaceSubmission\) \{[\s\S]{0,140}setInputDraft\(""\)/);
  assert.match(floating, /if \(!isResponseSurfaceSubmission\) \{[\s\S]{0,180}setInput\(""\)/);
  assert.match(embedded, /if \(turnChatMode\) resetChatModeAfterTurn\(\)/);
  assert.match(
    workspace,
    /if \(sendSucceeded && !isResponseSurfaceSubmission && requestChatMode\)/,
  );
  assert.match(floating, /if \(!isResponseSurfaceSubmission && requestChatMode\) resetChatModeAfterTurn\(\)/);
  assert.match(embedded, /\? responseSurfaceResult\s*:\s*true/);
  assert.match(floating, /\? responseSurfaceResult\s*:\s*true/);
  assert.match(workspace, /\? responseSurfaceResult\s*:\s*sendSucceeded/);
  assert.doesNotMatch(
    embedded,
    /\? \{ status: "succeeded", serverAccepted: true \}/,
    "embedded chat must preserve the store's durable acceptance evidence",
  );
  assert.doesNotMatch(
    floating,
    /\? \{ status: "succeeded", serverAccepted: true \}/,
    "floating chat must preserve the store's durable acceptance evidence",
  );
  assert.match(api, /form\.append\("response_surface_submission"/);
  assert.match(retry, /responseSurfaceSubmission\?: ResponseSurfaceSubmissionReceipt/);
});

test("chat history projects receipts into read-only HTML activity", async () => {
  const [history, renderer] = await Promise.all([
    source("src/pages/ChatHistory.tsx"),
    source("src/components/InteractiveResponseSurface.tsx"),
  ]);

  assert.match(history, /collectResponseSurfaceSubmissionReceipts/);
  assert.match(history, /isResponseSurfaceSubmissionMessage/);
  assert.match(history, /responseSurfaceSubmissionReceipts/);
  assert.match(history, /sourceMessageId=\{msg\.id\}/);
  assert.doesNotMatch(history, /onResponseSurfaceSubmit=/);
  assert.match(renderer, /const interactionReadOnly = !onSubmit/);
  assert.match(renderer, /busy: submittingRef\.current/);
});

test("response surface focus mode becomes full-screen on compact viewports", async () => {
  const [renderer, css, modal] = await Promise.all([
    source("src/components/InteractiveResponseSurface.tsx"),
    source("src/components/InteractiveResponseSurface.css"),
    source("src/components/ui/Modal.tsx"),
  ]);

  assert.match(css, /@media \(max-width: 640px\)/);
  assert.match(css, /response-surface-modal-overlay[\s\S]*?padding: 0 !important/);
  assert.match(css, /response-surface-modal-overlay[\s\S]*?height: 100dvh !important/);
  assert.match(css, /width: 100vw !important/);
  assert.match(css, /height: 100dvh !important/);
  assert.match(renderer, /overlayClassName="response-surface-modal-overlay"/);
  assert.match(modal, /overlayClassName/);
});
