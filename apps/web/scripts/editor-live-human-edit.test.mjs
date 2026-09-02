#!/usr/bin/env node
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { build } from "esbuild";

const bundled = await build({
  stdin: {
    contents: `
      export {
        buildEditorLiveDelimitedFrames,
        buildEditorLiveTextFrames,
        createEditorLiveDelimitedFrameStream,
      } from "../src/lib/editorLiveAnimation.ts";
      export {
        EDITOR_LIVE_SPREADSHEET_PAYLOAD_PREFIX,
        serializeEditorLiveSpreadsheetPayload,
      } from "../src/lib/editorLiveSpreadsheet.ts";
      export {
        AiEditPatchStreamEventKind,
        AiEditTargetKind,
        applyEditorLivePatch,
        containsEditorLivePatchProtocol,
        countCompleteEditorLivePatchOperations,
        createAiEditCommitCoordinator,
        createAiEditConversationDeleteCoordinator,
        createAiEditSessionCleanupCoordinator,
        createEditorLiveAdapter,
        createEditorLiveChatSseProjector,
        createEditorLivePatchDeltaQueue,
        createEditorLivePatchStream,
        createEditorLiveVisibleTextProjector,
        extractEditorLivePatchOperationPayloads,
        extractRecoverableEditorLivePatchOperationPayloads,
        EDITOR_LIVE_STREAMING_PREVIEW_MAX_CHARS,
        hasReviewableEditorLivePreview,
        isEditorLivePatchCommitAlreadyPreviewed,
        isSameEditorLiveTarget,
        mergeEditorLivePreviewDiff,
        nativeFilePatchResultFromSseFrame,
        nextEditorLiveChangeCount,
        shouldAttachEditorLiveSourceDocument,
        shouldStreamEditorLiveDeltaPreview,
        stripEditorLiveEditBlocks,
      } from "../src/lib/editorLiveChat.ts";
    `,
    loader: "tsx",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "esm",
  platform: "browser",
  write: false,
  logLevel: "silent",
});

const moduleUrl = `data:text/javascript;base64,${Buffer.from(
  bundled.outputFiles[0].text,
).toString("base64")}`;
const {
  AiEditPatchStreamEventKind,
  AiEditTargetKind,
  applyEditorLivePatch,
  buildEditorLiveDelimitedFrames,
  buildEditorLiveTextFrames,
  containsEditorLivePatchProtocol,
  countCompleteEditorLivePatchOperations,
  createAiEditCommitCoordinator,
  createAiEditConversationDeleteCoordinator,
  createAiEditSessionCleanupCoordinator,
  createEditorLiveDelimitedFrameStream,
  createEditorLiveAdapter,
  createEditorLiveChatSseProjector,
  createEditorLivePatchDeltaQueue,
  createEditorLivePatchStream,
  createEditorLiveVisibleTextProjector,
  extractEditorLivePatchOperationPayloads,
  extractRecoverableEditorLivePatchOperationPayloads,
  EDITOR_LIVE_STREAMING_PREVIEW_MAX_CHARS,
  EDITOR_LIVE_SPREADSHEET_PAYLOAD_PREFIX,
  hasReviewableEditorLivePreview,
  isEditorLivePatchCommitAlreadyPreviewed,
  isSameEditorLiveTarget,
  nativeFilePatchResultFromSseFrame,
  mergeEditorLivePreviewDiff,
  nextEditorLiveChangeCount,
  serializeEditorLiveSpreadsheetPayload,
  shouldAttachEditorLiveSourceDocument,
  shouldStreamEditorLiveDeltaPreview,
  stripEditorLiveEditBlocks,
} = await import(moduleUrl);

test("native PPT patch SSE frames require a successful persisted result", () => {
  const result = nativeFilePatchResultFromSseFrame({
    tool_call: {
      name: "patch_file",
      status: "success",
      result: JSON.stringify({
        patched: true,
        path: "Knowledge/pitch.pptx",
        document_id: "doc-1",
        source_sha256: "abc123",
        knowledge_synced: true,
      }),
    },
  });
  assert.deepEqual(result, {
    patched: true,
    path: "Knowledge/pitch.pptx",
    document_id: "doc-1",
    source_sha256: "abc123",
    knowledge_synced: true,
  });
  assert.equal(nativeFilePatchResultFromSseFrame({
    tool_call: { name: "patch_file", status: "pending", result: { patched: true, path: "x.pptx" } },
  }), null);
  assert.equal(nativeFilePatchResultFromSseFrame({
    tool_call: { name: "patch_file", status: "error", result: { patched: true, path: "x.pptx" } },
  }), null);
  assert.equal(nativeFilePatchResultFromSseFrame({
    tool_call: { name: "patch_file", status: "failed", result: { patched: true, path: "x.pptx" } },
  }), null);
  assert.equal(nativeFilePatchResultFromSseFrame({
    tool_call: { name: "generate_file", status: "success", result: { patched: true, path: "x.pptx" } },
  }), null);
  assert.equal(nativeFilePatchResultFromSseFrame({
    tool_call: { name: "patch_file", status: "success", result: "not-json" },
  }), null);
  assert.equal(nativeFilePatchResultFromSseFrame({
    tool_call: { name: "patch_file", status: "success", result: { error: "failed", patched: true, path: "x.pptx" } },
  }), null);
});

test("AI Edit Chat projection streams narration without typing hidden patch JSON", () => {
  const projector = createEditorLiveVisibleTextProjector();
  let source = "正在修改。 ";
  for (let index = 0; index < 7; index += 1) {
    const patch = JSON.stringify({
      op: "replace",
      find: `old-${index}`,
      replace: `hidden-${index}-${"payload".repeat(90)}`,
    });
    source += `<manor-live-patch>[${patch}]</manor-live-patch>`;
    if (index === 6) source += "\n\n已经完成。";
  }
  const chunks = Array.from(
    { length: Math.ceil(source.length / 3) },
    (_, index) => source.slice(index * 3, index * 3 + 3),
  );
  const projected = chunks.map((chunk) => projector.push(chunk)).join("")
    + projector.flush();

  assert.equal(projected, "正在修改。 \n\n已经完成。");
  assert.doesNotMatch(projected, /manor-live-patch|hidden-|payload|\"op\"/);
});

test("AI Edit Chat projection buffers ambiguous tags and resets with the model stream", () => {
  const projector = createEditorLiveVisibleTextProjector();
  assert.equal(projector.push("a <"), "a");
  assert.equal(projector.push("b"), " <b", "ordinary text is released once it cannot be a hidden tag");
  assert.equal(projector.push(" <"), "");
  assert.equal(projector.flush(), " <", "a visible dangling angle bracket is preserved at EOF");

  projector.reset();
  assert.equal(projector.push("retry"), "retry");
  assert.equal(projector.flush(), "");

  projector.reset();
  assert.equal(projector.push("a <<"), "a <");
  assert.equal(projector.push("manor live edit mode=\"preview\">secret"), "");
  assert.equal(projector.push("</manor-live-edit>b"), "b");
  assert.equal(projector.flush(), "");
});

test("AI Edit Chat incremental projection matches final visible-text semantics", () => {
  const sources = [
    "  leading and trailing text  ",
    "before <manor-live-patch>[hidden]</manor-live-patch> after ",
    "before <MANOR LIVE EDIT mode=\"preview\">hidden</MANOR LIVE EDIT> after",
    "a <<manor-live-patch>hidden</manor-live-patch>b",
    "plain <b>markup</b>",
    "draft <manor-live-pat",
    "draft <ma",
    "draft <",
  ];

  for (const source of sources) {
    for (let width = 1; width <= 7; width += 1) {
      const projector = createEditorLiveVisibleTextProjector();
      let projected = "";
      for (let offset = 0; offset < source.length; offset += width) {
        projected += projector.push(source.slice(offset, offset + width));
      }
      projected += projector.flush();
      assert.equal(projected, stripEditorLiveEditBlocks(source), `${source} at width ${width}`);
    }
  }
});

test("AI Edit Chat SSE projection resets hidden protocol at summary_start", () => {
  const projector = createEditorLiveChatSseProjector();
  const projected = [
    projector.projectLine("event: text_delta"),
    projector.projectLine(`data: ${JSON.stringify({
      text_delta: 'opening <manor-live-patch>[{"op":"replace"',
    })}`),
    projector.projectLine("event: summary_start"),
    projector.projectLine("data: {}"),
    projector.projectLine("event: text_delta"),
    projector.projectLine(`data: ${JSON.stringify({ text_delta: "Final answer" })}`),
    projector.projectLine("event: stream_end"),
    projector.flush(),
  ].join("\n");

  assert.match(projected, /"text_delta":"opening"/);
  assert.match(projected, /"text_delta":"Final answer"/);
  assert.doesNotMatch(projected, /manor-live-patch|\"op\"/);
});

test("live patches reject a mixed invalid payload atomically", () => {
  const result = applyEditorLivePatch("alpha", JSON.stringify([
    { op: "append", text: " beta" },
    { op: "replace", find: "beta" },
  ]));
  assert.equal(result.content, "alpha");
  assert.equal(result.applied, 0);
  assert.equal(result.failed[0]?.index, 1);
  assert.match(result.failed[0]?.reason || "", /replacement text/);
});

test("live patch operations are emitted before the array and tag finish", () => {
  const first = JSON.stringify({ op: "replace", find: "old", replace: "new" });
  const second = JSON.stringify({ op: "append", text: " done" });
  const partial = `<manor-live-patch>[${first},${second.slice(0, -2)}`;
  assert.deepEqual(extractEditorLivePatchOperationPayloads(partial), [first]);

  const stream = createEditorLivePatchStream();
  assert.deepEqual(stream.push("<manor-live-patch>["), []);
  const firstDeltaEvents = stream.push(first.slice(0, -1));
  assert.equal(firstDeltaEvents.at(-1)?.kind, AiEditPatchStreamEventKind.Delta);
  assert.equal(
    applyEditorLivePatch("old", firstDeltaEvents.at(-1)?.patch || "").content,
    "new",
  );
  assert.deepEqual(stream.push(`${first.at(-1)},`), [{
    kind: AiEditPatchStreamEventKind.Commit,
    operationIndex: 0,
    patch: first,
  }]);
  assert.deepEqual(stream.push(`${second}]</manor-live-patch>`), [{
    kind: AiEditPatchStreamEventKind.Commit,
    operationIndex: 1,
    patch: second,
  }]);
  assert.deepEqual(stream.push(" explanation"), [], "completed operations are never emitted twice");
  assert.equal(stream.operationCount(), 2);

  const invalidSeparator = `<manor-live-patch>[${first} garbage ${second}]`;
  assert.deepEqual(
    extractEditorLivePatchOperationPayloads(invalidSeparator),
    [first],
    "objects without the canonical array separator are not executable",
  );
  assert.equal(
    countCompleteEditorLivePatchOperations(invalidSeparator),
    null,
    "an invalid final array cannot make its streamed prefix reviewable",
  );
  assert.equal(countCompleteEditorLivePatchOperations(partial), null);
  assert.equal(
    countCompleteEditorLivePatchOperations(
      `<manor-live-patch>[${first}]</manor-live-patch><manor-live-patch`,
    ),
    null,
  );
  assert.equal(
    countCompleteEditorLivePatchOperations(
      `<manor-live-patch>[${first}]</manor-live-patch><manor-live-patchx`,
    ),
    null,
  );
  assert.equal(
    countCompleteEditorLivePatchOperations(
      `<manor-live-patch>[${first}]</manor-live-patch></manor-live-patch`,
    ),
    null,
  );
  assert.equal(
    countCompleteEditorLivePatchOperations(
      `<manor-live-patch>[${first}]</manor-live-patch><manor-live-patch>`,
    ),
    null,
    "a complete prefix cannot hide a dangling patch tag at EOF",
  );
  assert.equal(
    countCompleteEditorLivePatchOperations(
      `<manor-live-patch>[${first},${second}]</manor-live-patch>`,
    ),
    2,
  );
  assert.equal(
    extractRecoverableEditorLivePatchOperationPayloads(
      `<manor-live-patch>[${first}`,
    ),
    null,
    "a closed operation inside an unfinished protocol is never recoverable",
  );
  assert.deepEqual(
    extractRecoverableEditorLivePatchOperationPayloads(
      `<manor-live-patch>[${first},${second}]</manor-live-patch>`,
    ),
    [first, second],
  );
  assert.equal(containsEditorLivePatchProtocol("generated image"), false);
  assert.equal(containsEditorLivePatchProtocol("generated image <manor-live-pa"), true);
});

test("model patch text changes the temporary buffer before its JSON operation closes", () => {
  const stream = createEditorLivePatchStream();
  assert.deepEqual(
    stream.push('<manor-live-patch>[{"op":"append","text":"'),
    [{
      kind: AiEditPatchStreamEventKind.Delta,
      operationIndex: 0,
      patch: JSON.stringify({ op: "append", text: "" }),
    }],
  );

  const hello = stream.push("Hel");
  assert.equal(hello.length, 1);
  assert.equal(hello[0].kind, AiEditPatchStreamEventKind.Delta);
  assert.equal(applyEditorLivePatch("Start: ", hello[0].patch).content, "Start: Hel");
  assert.equal(stream.operationCount(), 0, "the editor delta precedes the operation commit");

  const escaped = stream.push('lo\\nworld\\uD83D');
  assert.equal(
    applyEditorLivePatch("Start: ", escaped[0].patch).content,
    "Start: Hello\nworld",
    "unfinished escapes and surrogate pairs stay buffered",
  );
  const emoji = stream.push('\\uDE00');
  assert.equal(
    applyEditorLivePatch("Start: ", emoji[0].patch).content,
    "Start: Hello\nworld😀",
  );

  const committed = stream.push('"}]</manor-live-patch>');
  assert.equal(committed.length, 1);
  assert.equal(committed[0].kind, AiEditPatchStreamEventKind.Commit);
  assert.equal(committed[0].operationIndex, 0);
  assert.equal(
    applyEditorLivePatch("Start: ", committed[0].patch).content,
    "Start: Hello\nworld😀",
  );
  assert.equal(stream.operationCount(), 1);
});

test("live patch parser reset discards an abandoned provider attempt", () => {
  const stream = createEditorLivePatchStream();
  const abandoned = stream.push('<manor-live-patch>[{"op":"append","text":"old');
  assert.equal(abandoned.at(-1)?.kind, AiEditPatchStreamEventKind.Delta);
  stream.reset();
  assert.equal(stream.operationCount(), 0);
  const replacement = JSON.stringify({ op: "append", text: "new" });
  assert.deepEqual(stream.push(`<manor-live-patch>[${replacement}]</manor-live-patch>`), [{
    kind: AiEditPatchStreamEventKind.Commit,
    operationIndex: 0,
    patch: replacement,
  }]);
});

test("a validated Commit does not replay content already shown by its Delta", () => {
  const before = JSON.stringify({ format: "manor-audio-edit-v1", volume: 1 });
  const after = JSON.stringify({ format: "manor-audio-edit-v1", volume: 0.5 });
  const stream = createEditorLivePatchStream();
  const openOperation = [
    '<manor-live-patch>[{"op":"replace","find":',
    JSON.stringify(before),
    ',"replace":',
    JSON.stringify(after).slice(0, -1),
  ].join("");
  const delta = stream.push(openOperation).at(-1);
  const commit = stream.push('"}]</manor-live-patch>').at(-1);
  assert.equal(delta?.kind, AiEditPatchStreamEventKind.Delta);
  assert.equal(commit?.kind, AiEditPatchStreamEventKind.Commit);
  const deltaContent = applyEditorLivePatch(before, delta?.patch || "").content;
  const commitContent = applyEditorLivePatch(before, commit?.patch || "").content;
  assert.equal(deltaContent, after);
  assert.equal(commitContent, after);
  assert.equal(
    isEditorLivePatchCommitAlreadyPreviewed(
      commit?.operationIndex ?? -1,
      delta?.operationIndex ?? null,
      commitContent,
      deltaContent,
    ),
    true,
  );
  assert.equal(
    isEditorLivePatchCommitAlreadyPreviewed(1, 0, commitContent, deltaContent),
    false,
    "content equality from another operation must not suppress its preview",
  );
});

test("live patch deltas coalesce to the newest value once per animation frame", async () => {
  const frames = [];
  const applied = [];
  const queue = createEditorLivePatchDeltaQueue(
    async (event) => applied.push(event.patch),
    (callback) => {
      frames.push(callback);
      return frames.length;
    },
    () => undefined,
  );
  const delta = (patch) => ({
    kind: AiEditPatchStreamEventKind.Delta,
    operationIndex: 0,
    patch,
  });

  queue.enqueue(delta("H"));
  queue.enqueue(delta("He"));
  queue.enqueue(delta("Hel"));
  assert.equal(frames.length, 1, "one frame is scheduled for a burst of model tokens");
  assert.deepEqual(applied, []);
  frames.shift()(0);
  await queue.flush();
  assert.deepEqual(applied, ["Hel"], "obsolete token deltas are never replayed after the model advances");

  queue.enqueue(delta("Hell"));
  queue.enqueue(delta("Hello"));
  await queue.flush();
  assert.deepEqual(applied, ["Hel", "Hello"], "Commit flushes only the newest pending delta");
  queue.enqueue(delta("abandoned"));
  await queue.reset();
  assert.deepEqual(applied, ["Hel", "Hello"], "reset removes the previous provider attempt");
  queue.enqueue(delta("replacement"));
  await queue.flush();
  assert.deepEqual(applied, ["Hel", "Hello", "replacement"]);
  await queue.cancel();
});

test("pending change counts and diffs accumulate across AI Edit turns", () => {
  const previous = { changeCount: 1 };
  const firstDeltaOfFollowUp = {
    complete: false,
    phase: "preview",
    source: "assistant-stream",
    patchCount: 1,
    turnBasePatchCount: 1,
  };
  assert.equal(nextEditorLiveChangeCount(previous, firstDeltaOfFollowUp), 2);
  assert.equal(
    nextEditorLiveChangeCount({ changeCount: 2 }, firstDeltaOfFollowUp),
    2,
    "the commit for the same operation must not count the streamed delta twice",
  );
  assert.equal(
    mergeEditorLivePreviewDiff("first turn diff", {
      ...firstDeltaOfFollowUp,
      complete: true,
      phase: "complete",
      diff: "second turn diff",
    }),
    "first turn diff\n\nsecond turn diff",
  );
});

test("reviewable AI Edit state is derived from persisted change count", () => {
  assert.equal(hasReviewableEditorLivePreview(null), false);
  assert.equal(hasReviewableEditorLivePreview({ changeCount: 0 }), false);
  assert.equal(hasReviewableEditorLivePreview({ changeCount: 1 }), true);
});

test("large documents skip per-token full-document previews but still allow bounded documents", () => {
  assert.equal(
    shouldStreamEditorLiveDeltaPreview(EDITOR_LIVE_STREAMING_PREVIEW_MAX_CHARS),
    true,
  );
  assert.equal(
    shouldStreamEditorLiveDeltaPreview(EDITOR_LIVE_STREAMING_PREVIEW_MAX_CHARS + 1),
    false,
  );
});

function liveEditDetail(kind, id, metadata = {}) {
  return {
    ...metadata,
    adapter: createEditorLiveAdapter({
      target: { kind, id },
      read: () => "",
      getTurnPreviewState: () => ({ changeCount: 0 }),
      beginTurn: () => true,
      preview: () => true,
      complete: () => true,
      rollback: () => undefined,
    }),
  };
}

test("live editor frames select, delete, type, and settle in the original buffer", () => {
  const frames = buildEditorLiveTextFrames("Hello old world", "Hello new world");
  assert.deepEqual(frames.map((frame) => frame.phase), [
    "select",
    "delete",
    "delete",
    "delete",
    "type",
    "type",
    "type",
    "settle",
  ]);
  assert.equal(frames[0].content.slice(frames[0].selectionStart, frames[0].selectionEnd), "old");
  assert.equal(frames.at(-1).content, "Hello new world");
});

test("live editor frames preserve Unicode boundaries and bound large edits", () => {
  const unicodeFrames = buildEditorLiveTextFrames("Hi 👋", "Hi 🌍!");
  assert.equal(unicodeFrames.at(-1).content, "Hi 🌍!");
  assert.ok(unicodeFrames.every((frame) => !frame.content.includes("�")));

  const largeFrames = buildEditorLiveTextFrames("a".repeat(10_000), "b".repeat(10_000));
  assert.ok(largeFrames.length <= 74);
  assert.equal(largeFrames.at(-1).content, "b".repeat(10_000));
});

test("live spreadsheet frames preserve typed rows and settle on the target data", () => {
  const format = { delimiter: ",", lineEnding: "\n", finalLineEnding: false };
  const before = [["Name", "Old"]];
  const after = [["Name", "New"]];
  const frames = buildEditorLiveDelimitedFrames(before, after, format);
  assert.equal(frames[0].phase, "select");
  assert.ok(frames.some((frame) => frame.phase === "delete"));
  assert.ok(frames.some((frame) => frame.phase === "type"));
  assert.equal(frames.at(-1)?.phase, "settle");
  assert.deepEqual(frames[0].rows, before);
  assert.deepEqual(frames.at(-1).rows, after);
  assert.ok(frames.length <= 74);
});

test("spreadsheet append frames create a row and type its cells progressively", () => {
  const frames = buildEditorLiveDelimitedFrames(
    [["Name", "Value"]],
    [["Name", "Value"], ["Delta", "40"]],
    { delimiter: ",", lineEnding: "\n", finalLineEnding: false },
  );
  assert.ok(frames.length > 4);
  assert.ok(frames.some((frame) => frame.rows[1]?.[0] === ""));
  assert.ok(frames.some((frame) => frame.rows[1]?.[0] === "D" && frame.rows[1]?.[1] === undefined));
  assert.ok(frames.some((frame) => frame.rows[1]?.[0] === "Delta" && frame.rows[1]?.[1] === "4"));
  assert.deepEqual(frames.at(-1)?.rows, [["Name", "Value"], ["Delta", "40"]]);
});

test("spreadsheet frames insert blank rows at their real position", () => {
  const format = { delimiter: ",", lineEnding: "\n", finalLineEnding: false };
  for (const { before, after, insertAt, forbidden } of [
    {
      before: [["A"], ["B"]],
      after: [["X"], ["A"], ["B"]],
      insertAt: 0,
      forbidden: "XA",
    },
    {
      before: [["A"], ["B"]],
      after: [["A"], ["X"], ["B"]],
      insertAt: 1,
      forbidden: "XB",
    },
  ]) {
    const frames = buildEditorLiveDelimitedFrames(before, after, format);
    assert.ok(frames.some((frame) => frame.rows[insertAt]?.[0] === ""));
    assert.ok(frames.every((frame) => frame.rows.flat().every((cell) => cell !== forbidden)));
    assert.deepEqual(frames.at(-1)?.rows, after);
  }
});

test("large spreadsheet frame streams stay bounded and lazy", () => {
  const before = Array.from({ length: 10_000 }, (_, row) => (
    Array.from({ length: 10 }, (_, column) => `${row}:${column}`)
  ));
  const after = [...before, Array.from({ length: 10 }, (_, column) => `new:${column}`)];
  const stream = createEditorLiveDelimitedFrameStream(
    before,
    after,
    { delimiter: ",", lineEnding: "\n", finalLineEnding: false },
  );
  const first = stream.next();
  assert.equal(first.done, false);
  assert.equal(first.value.rows.length, 10_001);
  assert.strictEqual(first.value.rows[0], before[0], "unchanged rows should be structurally shared");
  const remaining = [...stream];
  assert.ok(remaining.length <= 2, "large sheets should not enqueue dozens of full-grid renders");
  assert.deepEqual(remaining.at(-1)?.rows || first.value.rows, after);
});

test("XLSX live-edit payloads preserve numeric cell types", () => {
  const payload = serializeEditorLiveSpreadsheetPayload(
    [["Name", "Value"], ["Delta", 40]],
    [],
    {},
  );
  assert.ok(payload.startsWith(EDITOR_LIVE_SPREADSHEET_PAYLOAD_PREFIX));
  const parsed = JSON.parse(payload.slice(EDITOR_LIVE_SPREADSHEET_PAYLOAD_PREFIX.length));
  assert.equal(parsed.data[1][1], 40);
  assert.equal(typeof parsed.data[1][1], "number");
});

test("reopening AI Edit preserves only the same target session", () => {
  assert.equal(
    isSameEditorLiveTarget(
      liveEditDetail(AiEditTargetKind.Document, "doc-1", { sourcePath: "/editor/doc-1" }),
      liveEditDetail(AiEditTargetKind.Document, "doc-1", { sourcePath: "/renamed/doc-1" }),
    ),
    true,
  );
  assert.equal(
    isSameEditorLiveTarget(
      liveEditDetail(AiEditTargetKind.Document, "doc-1"),
      liveEditDetail(AiEditTargetKind.Document, "doc-2"),
    ),
    false,
  );
  assert.equal(
    isSameEditorLiveTarget(
      liveEditDetail(AiEditTargetKind.Workflow, "flow-1", { documentName: "Publish" }),
      liveEditDetail(AiEditTargetKind.Workflow, "flow-1", { documentName: "Renamed" }),
    ),
    true,
  );
  assert.equal(
    isSameEditorLiveTarget(
      liveEditDetail(AiEditTargetKind.Workflow, "flow-1"),
      liveEditDetail(AiEditTargetKind.Workflow, "flow-2"),
    ),
    false,
  );
});

test("AI Edit adapter factory owns the target enum and requires full lifecycle hooks", async () => {
  assert.deepEqual(Object.values(AiEditTargetKind).sort(), [
    "audio",
    "diagram",
    "document",
    "image",
    "project",
    "video",
    "workflow",
  ]);
  assert.throws(
    () => createEditorLiveAdapter({
      target: { kind: AiEditTargetKind.Document, id: "   " },
      read: () => "",
      preview: () => true,
      complete: () => true,
      rollback: () => undefined,
    }),
    /stable target id/,
  );
  assert.throws(
    () => createEditorLiveAdapter({
      target: { kind: "unsupported", id: "resource-1" },
      read: () => "",
      preview: () => true,
      complete: () => true,
      rollback: () => undefined,
    }),
    /supported target kind/,
  );
  assert.throws(
    () => createEditorLiveAdapter({
      target: { kind: AiEditTargetKind.Image, id: "image-1" },
      read: () => "",
      preview: () => true,
    }),
    /begin, complete, and rollback lifecycle hooks/,
  );
  assert.throws(
    () => createEditorLiveAdapter({
      target: { kind: AiEditTargetKind.Image, id: "image-1" },
      read: () => "",
      beginTurn: () => true,
      preview: () => true,
      complete: () => true,
      rollback: () => undefined,
    }),
    /turn preview state hook/,
  );
  let completed = false;
  let began = false;
  let previewedContent = "";
  let rolledBack = false;
  let changeCount = 2;
  const adapter = createEditorLiveAdapter({
    target: { kind: AiEditTargetKind.Project, id: " project:file " },
    read: () => "source",
    getTurnPreviewState: () => ({ changeCount }),
    beginTurn: () => { began = true; return true; },
    preview: (content) => { previewedContent = content; return true; },
    complete: () => { completed = true; },
    rollback: () => { rolledBack = true; },
  });
  assert.equal(adapter.target.id, "project:file");
  assert.equal(adapter.read(), "source");
  assert.deepEqual(adapter.getTurnPreviewState(), { changeCount: 2 });
  changeCount = 3;
  assert.deepEqual(adapter.getTurnPreviewState(), { changeCount: 3 });
  await adapter.beginTurn({
    complete: false,
    phase: "preview",
    source: "assistant-stream",
  });
  await adapter.complete("source", {
    complete: true,
    phase: "complete",
    source: "assistant-stream",
  });
  await adapter.restore("previous preview", {
    complete: true,
    phase: "complete",
    source: "assistant-stream",
  });
  await adapter.rollback();
  assert.equal(previewedContent, "previous preview");
  assert.equal(began, true);
  assert.equal(completed, true);
  assert.equal(rolledBack, true);
});

test("AI Edit commit coordinator makes close wait for the active Accept", async () => {
  const commitCoordinator = createAiEditCommitCoordinator();
  let releaseCommit;
  const commit = commitCoordinator.run(() => new Promise((resolve) => {
    releaseCommit = resolve;
  }));
  assert.equal(commitCoordinator.isCommitting(), true);

  let closeCanContinue = false;
  const waitingClose = commitCoordinator.waitForCommit().then(() => {
    closeCanContinue = true;
  });
  await Promise.resolve();
  assert.equal(closeCanContinue, false);
  await assert.rejects(
    commitCoordinator.run(async () => undefined),
    /already accepting/,
  );

  releaseCommit();
  await Promise.all([commit, waitingClose]);
  assert.equal(closeCanContinue, true);
  assert.equal(commitCoordinator.isCommitting(), false);

  const adapter = createEditorLiveAdapter({
    target: { kind: AiEditTargetKind.Document, id: "doc-commit" },
    read: () => "",
    getTurnPreviewState: () => ({ changeCount: 0 }),
    beginTurn: () => true,
    preview: () => true,
    complete: () => true,
    rollback: () => undefined,
    commitCoordinator,
  });
  assert.equal(adapter.isCommitting(), false);
  await adapter.waitForCommit();

  let releaseAccept;
  const accepting = commitCoordinator.run(() => new Promise((resolve) => {
    releaseAccept = resolve;
  }));
  await Promise.resolve();
  let previewCalls = 0;
  const queuedPreview = adapter.preview("next patch", {
    complete: false,
    phase: "preview",
    source: "assistant-stream",
  }).then(() => {
    previewCalls += 1;
  });
  await Promise.resolve();
  assert.equal(previewCalls, 0, "a streamed patch waits while Accept persists the prior preview");
  releaseAccept();
  await Promise.all([accepting, queuedPreview]);
  assert.equal(previewCalls, 1, "the streamed patch resumes instead of being rejected and lost");
});

test("aborted previews do not resume after waiting for an active Accept", async () => {
  const commitCoordinator = createAiEditCommitCoordinator();
  let releaseAccept;
  const accepting = commitCoordinator.run(() => new Promise((resolve) => {
    releaseAccept = resolve;
  }));
  await Promise.resolve();
  let previewCalls = 0;
  let completeCalls = 0;
  const adapter = createEditorLiveAdapter({
    target: { kind: AiEditTargetKind.Document, id: "doc-abort" },
    read: () => "",
    getTurnPreviewState: () => ({ changeCount: 0 }),
    beginTurn: () => true,
    preview: () => { previewCalls += 1; return true; },
    complete: () => { completeCalls += 1; return true; },
    rollback: () => undefined,
    commitCoordinator,
  });
  const controller = new AbortController();
  const preview = adapter.preview("next", {
    complete: false,
    phase: "preview",
    source: "assistant-stream",
    signal: controller.signal,
  });
  const complete = adapter.complete("next", {
    complete: true,
    phase: "complete",
    source: "assistant-stream",
    signal: controller.signal,
  });
  controller.abort();
  releaseAccept();
  assert.deepEqual(await Promise.all([accepting, preview, complete]), [undefined, false, false]);
  assert.equal(previewCalls, 0);
  assert.equal(completeCalls, 0);
});

test("AI Edit source attachments are limited to domains that need the original binary", () => {
  const source = (kind, overrides = {}) => ({
    documentId: "doc-1",
    documentName: "source.bin",
    fileType: "txt",
    ...overrides,
    adapter: liveEditDetail(kind, "target-1").adapter,
  });
  assert.equal(shouldAttachEditorLiveSourceDocument(source(AiEditTargetKind.Document)), false);
  assert.equal(
    shouldAttachEditorLiveSourceDocument(source(AiEditTargetKind.Document, { fileType: "pdf" })),
    true,
  );
  assert.equal(
    shouldAttachEditorLiveSourceDocument(source(AiEditTargetKind.Document, {
      fileType: undefined,
      mimeType: "application/pdf",
    })),
    true,
  );
  assert.equal(shouldAttachEditorLiveSourceDocument(source(AiEditTargetKind.Audio)), true);
  assert.equal(shouldAttachEditorLiveSourceDocument(source(AiEditTargetKind.Video)), true);
  assert.equal(
    shouldAttachEditorLiveSourceDocument(source(AiEditTargetKind.Image, {
      getAttachmentFiles: async () => [],
    })),
    false,
  );
});

test("AI Edit conversation cleanup is single-flight per owner and conversation", async () => {
  const coordinator = createAiEditConversationDeleteCoordinator();
  const ownerA = { userId: "user-1", entityId: "entity-1" };
  const ownerB = { userId: "user-1", entityId: "entity-2" };
  let calls = 0;
  let releaseDelete;
  const operation = () => {
    calls += 1;
    return new Promise((resolve) => {
      releaseDelete = resolve;
    });
  };

  const first = coordinator.run(ownerA, "conversation-1", operation);
  const duplicate = coordinator.run(ownerA, "conversation-1", operation);
  const other = coordinator.run(ownerA, "conversation-2", async () => true);
  const otherOwner = coordinator.run(ownerB, "conversation-1", async () => true);
  await Promise.resolve();

  assert.strictEqual(duplicate, first);
  assert.equal(calls, 1, "the duplicate close and queue flush share one DELETE");
  assert.equal(await other, true, "another conversation is not blocked");
  assert.equal(await otherOwner, true, "the same id in another entity has a separate owner key");

  releaseDelete(true);
  assert.equal(await first, true);
  assert.equal(calls, 1);

  assert.equal(await coordinator.run(ownerA, "conversation-1", async () => {
    calls += 1;
    return true;
  }), true);
  assert.equal(
    calls,
    1,
    "a later close or cleanup-queue flush must not DELETE an already removed conversation",
  );

  let failedCleanupCalls = 0;
  assert.equal(await coordinator.run(ownerA, "conversation-failed", async () => {
    failedCleanupCalls += 1;
    return false;
  }), false);
  assert.equal(await coordinator.run(ownerA, "conversation-failed", async () => {
    failedCleanupCalls += 1;
    return true;
  }), true);
  assert.equal(failedCleanupCalls, 2, "an unsuccessful cleanup remains retryable");
});

test("AI Edit session cleanup is one shared transaction across close entrypoints", async () => {
  const coordinator = createAiEditSessionCleanupCoordinator();
  let calls = 0;
  let releaseCleanup;
  const operation = () => {
    calls += 1;
    return new Promise((resolve) => {
      releaseCleanup = resolve;
    });
  };

  const close = coordinator.run(operation);
  const accountChange = coordinator.run(operation);
  await Promise.resolve();

  assert.strictEqual(accountChange, close);
  assert.strictEqual(coordinator.current(), close);
  assert.equal(calls, 1, "close and account change must not rollback twice");

  releaseCleanup(true);
  assert.equal(await close, true);
  assert.equal(coordinator.current(), null);
});

test("chat streaming exposes identity before the first SSE frame and aborts both readers", async () => {
  const [storeSource, apiSource, floatingChatSource, liveChatSource] = await Promise.all([
    readFile(new URL("../src/stores/chatStream.ts", import.meta.url), "utf8"),
    readFile(new URL("../src/lib/api.ts", import.meta.url), "utf8"),
    readFile(new URL("../src/components/FloatingChat.tsx", import.meta.url), "utf8"),
    readFile(new URL("../src/lib/editorLiveChat.ts", import.meta.url), "utf8"),
  ]);

  assert.match(storeSource, /fetchFn\(ac\.signal\)/);
  assert.match(storeSource, /headers\.get\("X-Conversation-ID"\)[\s\S]*?setConvId\(responseConversationId\)/);
  assert.match(storeSource, /sessions\[liveKey\]\?\.convId === newId\) return/);
  assert.match(storeSource, /processSSEStream\([\s\S]*?get\(\)\.sessions\[liveKey\]\?\.convId \|\| convId/);
  assert.match(storeSource, /headers\.get\("X-Runtime-Run-ID"\)[\s\S]*?applyRuntimeState/);
  assert.match(
    apiSource,
    /stream: async \([\s\S]*?signal\?: AbortSignal;[\s\S]*?requestStreamResponse\("\/chat\/stream", form, opts\?\.signal\)/,
  );

  const pipeStart = floatingChatSource.indexOf("function pipeEditorLiveEditStream");
  const pipeEnd = floatingChatSource.indexOf("function hasApprovalRequest", pipeStart);
  const pipeSource = floatingChatSource.slice(pipeStart, pipeEnd);
  assert.match(pipeSource, /addEventListener\("abort", cancelLiveReader/);
  assert.match(pipeSource, /void reader\.cancel\(\)/);
  assert.match(pipeSource, /removeEventListener\("abort", cancelLiveReader\)/);
  assert.equal(
    (floatingChatSource.match(/if \(!ownsLiveEditTurn\(\)\) return false;/g) || []).length,
    3,
    "close or target switch is rechecked after async attachment work and before streaming",
  );
  assert.match(
    floatingChatSource,
    /editorLiveSessionTokenRef\.current === liveEditSessionToken/,
  );
  assert.doesNotMatch(liveChatSource, /localEditContent/);
});

test("stream orchestration previews each operation once and completes only after EOF", async () => {
  const [floatingChatSource, flowSource, editorSource, liveChatSource] = await Promise.all([
    readFile(new URL("../src/components/FloatingChat.tsx", import.meta.url), "utf8"),
    readFile(new URL("../src/pages/Flows.tsx", import.meta.url), "utf8"),
    readFile(new URL("../src/pages/DocEditor.tsx", import.meta.url), "utf8"),
    readFile(new URL("../src/lib/editorLiveChat.ts", import.meta.url), "utf8"),
  ]);
  const pipeStart = floatingChatSource.indexOf("function pipeEditorLiveEditStream");
  const pipeEnd = floatingChatSource.indexOf("function hasApprovalRequest", pipeStart);
  const pipeSource = floatingChatSource.slice(pipeStart, pipeEnd);
  assert.match(pipeSource, /createEditorLivePatchStream\(\)/);
  assert.match(pipeSource, /applyPatchStreamEvents\(patchStream\.push\(token\)\)/);
  assert.match(pipeSource, /createEditorLivePatchDeltaQueue\(/);
  assert.match(pipeSource, /shouldStreamEditorLiveDeltaPreview\(workingContent\.length\)/);
  assert.match(pipeSource, /await deltaPreviewQueue\.flush\(\)/);
  assert.match(pipeSource, /event\.kind === AiEditPatchStreamEventKind\.Commit/);
  assert.match(pipeSource, /streamEvent: AiEditPatchStreamEventKind\.Delta/);
  assert.match(pipeSource, /isEditorLivePatchCommitAlreadyPreviewed\(/);
  assert.match(
    pipeSource,
    /const continuesStreamedDelta = event\.operationIndex === lastDeltaOperationIndex;[\s\S]*?streamEvent: continuesStreamedDelta[\s\S]*?AiEditPatchStreamEventKind\.Delta/,
  );
  assert.match(pipeSource, /patchCount: appliedPatchCount \+ 1,[\s\S]*?sourceLabel: "assistant delta"/);
  const streamedDeltaStart = pipeSource.indexOf("const applyPatchDelta");
  const streamedDeltaEnd = pipeSource.indexOf("const deltaPreviewQueue", streamedDeltaStart);
  assert.doesNotMatch(
    pipeSource.slice(streamedDeltaStart, streamedDeltaEnd),
    /adapter\.read\(\)/,
    "a streamed Delta must not recapture the turn checkpoint or retarget a Project file",
  );
  assert.match(pipeSource, /const liveProcessing = \(async \(\) =>/);
  assert.match(pipeSource, /createEditorLiveChatSseProjector\(\)/);
  assert.doesNotMatch(pipeSource, /controller\.enqueue\(chunk\)/);
  const chatTransformStart = pipeSource.indexOf("transform(chunk, controller)");
  const chatFlushStart = pipeSource.indexOf("async flush(controller)", chatTransformStart);
  const chatTransformSource = pipeSource.slice(chatTransformStart, chatFlushStart);
  const chatFlushSource = pipeSource.slice(chatFlushStart);
  assert.match(chatTransformSource, /enqueueProjectedChatText\(decoded, controller\)/);
  assert.doesNotMatch(chatTransformSource, /await liveProcessing/);
  assert.match(
    chatFlushSource,
    /enqueueProjectedChatText\(chatProtocolDecoder\.decode\(\), controller, true\)[\s\S]*?await liveProcessing/,
  );
  assert.match(pipeSource, /buffer \+= decoder\.decode\(\);[\s\S]*?if \(buffer\) await processSseLine\(buffer\)/);
  assert.match(pipeSource, /if \(currentEvent === "error"\) sawStreamError = true/);
  assert.match(pipeSource, /if \(currentEvent === "text_reset"\)[\s\S]*?await resetPatchStreamAttempt\(\)/);
  assert.match(pipeSource, /await deltaPreviewQueue\.reset\(\)[\s\S]*?patchStream\.reset\(\)/);
  assert.match(pipeSource, /operationCount: processedOperationCount/);
  assert.match(pipeSource, /detail\.adapter\.preview\(result\.content, \{[\s\S]*?complete: false,[\s\S]*?AiEditApplyPhase\.Preview/);
  assert.match(pipeSource, /while \(true\)[\s\S]*?hasCompletePatchProtocol[\s\S]*?await finishLiveEdit\(/);
  assert.match(pipeSource, /countCompleteEditorLivePatchOperations\(assistantText\)/);
  assert.match(pipeSource, /await rollbackFailedLiveEdit\(\)/);
  assert.match(pipeSource, /await detail\.adapter\.complete\(workingContent, \{[\s\S]*?complete: true,[\s\S]*?AiEditApplyPhase\.Complete/);
  assert.match(pipeSource, /await detail\.adapter\.preview\(result\.content[\s\S]*?if \(!isActive\(\) \|\| signal\?\.aborted\) return;[\s\S]*?workingContent = result\.content/);
  assert.match(pipeSource, /const baselineContent = initialContent/);
  assert.doesNotMatch(pipeSource, /applyAssistantReplacementEdit|applyFallbackEdit|local fallback/);
  assert.doesNotMatch(pipeSource, /currentSessionKeyRef\.current === sessionKey/);

  const patchStreamStart = liveChatSource.indexOf("export function createEditorLivePatchStream");
  const patchStreamEnd = liveChatSource.indexOf(
    "export function createEditorLivePatchDeltaQueue",
    patchStreamStart,
  );
  const patchStreamSource = liveChatSource.slice(patchStreamStart, patchStreamEnd);
  assert.match(patchStreamSource, /let buffer = ""/);
  assert.doesNotMatch(patchStreamSource, /source \+= chunk/);
  assert.doesNotMatch(patchStreamSource, /extractEditorLivePatchOperationPayloads\(source\)/);
  assert.doesNotMatch(patchStreamSource, /activePatchOperationPrefix/);

  const visibleProjectorStart = liveChatSource.indexOf(
    "export function createEditorLiveVisibleTextProjector",
  );
  const visibleProjectorEnd = liveChatSource.indexOf(
    "export function createEditorLiveChatSseProjector",
    visibleProjectorStart,
  );
  const visibleProjectorSource = liveChatSource.slice(
    visibleProjectorStart,
    visibleProjectorEnd,
  );
  assert.match(visibleProjectorSource, /for \(const char of chunk\)/);
  assert.doesNotMatch(visibleProjectorSource, /source \+= chunk/);
  assert.doesNotMatch(visibleProjectorSource, /stripEditorLiveEditBlocks\(source\)/);

  const chatSseProjectorStart = visibleProjectorEnd;
  const chatSseProjectorEnd = liveChatSource.indexOf(
    "export function containsEditorLivePatchProtocol",
    chatSseProjectorStart,
  );
  const chatSseProjectorSource = liveChatSource.slice(
    chatSseProjectorStart,
    chatSseProjectorEnd,
  );
  assert.match(chatSseProjectorSource, /nextEvent === "text_reset" \|\| nextEvent === "summary_start"/);
  assert.match(chatSseProjectorSource, /visibleText\.reset\(\)/);

  const recoveryStart = floatingChatSource.indexOf("if (!editorLiveSessionActive) return;", floatingChatSource.indexOf("/* Opening the panel"));
  const recoveryEnd = floatingChatSource.indexOf("/* Listen for video completion", recoveryStart);
  const recoverySource = floatingChatSource.slice(recoveryStart, recoveryEnd);
  assert.match(recoverySource, /\["pending", "failed"\]\.includes\(turn\.phase\)/);
  assert.match(recoverySource, /extractRecoverableEditorLivePatchOperationPayloads\(assistantContent\)/);
  assert.match(recoverySource, /allPayloads\.slice\(processedOperationCount\)/);
  assert.match(recoverySource, /canFinalizeRecoveredPreview/);
  assert.match(recoverySource, /recoveredAppliedPatchCount/);
  assert.match(recoverySource, /editorLiveAppliedRef\.current = \{ \.\.\.turn, phase: "streaming" \}/);

  const failedSendStart = floatingChatSource.indexOf("if (!sendSucceeded)");
  const failedSendEnd = floatingChatSource.indexOf("clearPendingChatRetry", failedSendStart);
  const failedSendSource = floatingChatSource.slice(failedSendStart, failedSendEnd);
  assert.match(failedSendSource, /applied\.abortController\?\.abort\(\)/);
  assert.match(failedSendSource, /await restoreEditorLiveTurn\(/);
  assert.match(failedSendSource, /restoredPreviousPreview \? "complete" : "failed"/);

  const workflowOpen = flowSource.slice(
    flowSource.indexOf("const discardWorkflowAiPreview"),
    flowSource.indexOf("// Add a node", flowSource.indexOf("const discardWorkflowAiPreview")),
  );
  assert.match(workflowOpen, /await updateMutation\.mutateAsync/);
  assert.match(workflowOpen, /workflowAiCommitCoordinator\.run/);
  assert.match(workflowOpen, /commitCoordinator: workflowAiCommitCoordinator/);
  const workflowComplete = workflowOpen.slice(workflowOpen.indexOf("complete:"));
  assert.doesNotMatch(workflowComplete, /updateMutation\.mutate\(/);
  assert.match(workflowComplete, /status: AiEditPreviewStatus\.Ready/);

  assert.match(editorSource, /mode === "code"[\s\S]*?AiEditTargetKind\.Project/);
  assert.match(editorSource, /const targetId = docId/);
  assert.match(editorSource, /const editorLiveTurnPreviewCheckpointRef = useRef<EditorLivePreviewState/);
  assert.match(editorSource, /editorLiveTurnPreviewCheckpointRef\.current = cloneEditorLivePreviewState/);
  assert.match(editorSource, /const restoreEditorLiveTurnPreview = useCallback/);
  assert.match(editorSource, /checkpoint\.status !== AiEditPreviewStatus\.Ready/);
  assert.match(editorSource, /liveEditPreviewRef\.current = checkpoint/);
  assert.match(editorSource, /restore: \(_next, meta\) => restoreEditorLiveTurnPreview\(targetId, meta\)/);
  assert.match(editorSource, /if \(!docId \|\| !doc \|\| isLoadingContent\) return;/);
  assert.match(editorSource, /<AiEditButton[\s\S]*?disabled=\{!doc \|\| isLoadingContent\}/);
  assert.match(editorSource, /editorLiveTurnTargetPathRef\.current = targetPath/);
  assert.match(editorSource, /getTurnMetadata: \(\) => \{/);
  assert.match(editorSource, /getTurnPreviewState: \(\) => \(\{/);
  assert.match(editorSource, /beginTurn: beginEditorLiveTurn/);
  assert.match(editorSource, /documentName: currentDocumentName/);
  assert.match(editorSource, /codeLanguageForFile\(currentDocumentName\)/);
  assert.match(editorSource, /pendingPreview\?\.mode === "code" && pendingPreview\.targetPath/);
  assert.match(editorSource, /mode === "code" \? editorLiveTurnTargetPathRef\.current : undefined/);
  assert.doesNotMatch(editorSource, /`\$\{docId\}:file:/);
  assert.match(editorSource, /sheetDataRef\.current = data;[\s\S]*?sheetChartsRef\.current = charts;[\s\S]*?sheetStylesRef\.current = styles;/);
  assert.match(editorSource, /const animateEditorLiveSpreadsheet = useCallback\(async/);
  assert.match(editorSource, /createEditorLiveDelimitedFrameStream\([\s\S]*?beforeData,[\s\S]*?nextData/);
  assert.match(editorSource, /previewEditorLiveSheetFrame\(frame\.rows\)/);
  assert.match(editorSource, /if \(!persistCharts\) return sheetDataToCsv\(data\)/);
  assert.match(editorSource, /handleSheetChange\(nextData, nextCharts, nextStyles, undefined, false\)/);
  assert.match(editorSource, /mode === "spreadsheet"[\s\S]*?animateEditorLiveSpreadsheet\(nextText, targetId, targetPath, meta\)/);
  assert.match(editorSource, /status: AiEditPreviewStatus\.Ready/);
});


test("all visual media AI Edit surfaces implement preview, complete, rollback, and Accept", async () => {
  const [fileViewerSource, diagramSource, videoSource, controlsSource, flowSource] = await Promise.all([
    readFile(new URL("../src/pages/FileViewer.tsx", import.meta.url), "utf8"),
    readFile(new URL("../src/pages/DiagramStudio.tsx", import.meta.url), "utf8"),
    readFile(new URL("../src/pages/VideoEditor.tsx", import.meta.url), "utf8"),
    readFile(new URL("../src/components/ui/AiEditPreviewControls.tsx", import.meta.url), "utf8"),
    readFile(new URL("../src/pages/Flows.tsx", import.meta.url), "utf8"),
  ]);
  for (const bridge of ["pdfLiveEditBridge", "imageLiveEditBridge", "audioLiveEditBridge"]) {
    assert.match(fileViewerSource, new RegExp(`complete: ${bridge}\\.complete`));
    assert.match(fileViewerSource, new RegExp(`rollback: ${bridge}\\.rollback`));
    assert.match(fileViewerSource, new RegExp(`commitCoordinator: ${bridge}\\.commitCoordinator`));
    assert.match(fileViewerSource, new RegExp(`getTurnPreviewState: ${bridge}\\.getTurnPreviewState`));
  }
  for (const source of [fileViewerSource, diagramSource, videoSource, flowSource]) {
    assert.match(source, /nextEditorLiveChangeCount\(/);
    assert.match(source, /getTurnPreviewState/);
  }
  assert.match(fileViewerSource, /restore: imageLiveEditBridge\.restore/);
  assert.match(fileViewerSource, /const imageAiTurnCheckpointRef = useRef<ImageAiTurnCheckpoint/);
  assert.match(fileViewerSource, /const restoreImageAiTurn = useCallback/);
  assert.match(fileViewerSource, /generatedPreviewUrlRef\.current !== protectedSourceUrl/);
  assert.match(fileViewerSource, /status: AiEditPreviewStatus\.Ready/);
  assert.match(fileViewerSource, /updateEditorLiveChat\(fileViewerAiEditDetail\)/);
  assert.match(fileViewerSource, /pdfAiCommitCoordinator\.run/);
  assert.match(fileViewerSource, /imageAiCommitCoordinator\.run/);
  assert.match(fileViewerSource, /audioAiCommitCoordinator\.run/);
  assert.match(fileViewerSource, /AiEditTargetKind\.Audio/);
  assert.match(fileViewerSource, /renderAudioEditBlob/);
  assert.match(fileViewerSource, /api\.documents\.replaceFile\(docId/);
  assert.match(fileViewerSource, /<AiEditPreviewControls[\s\S]*?acceptAudioAiPreview/);
  assert.match(diagramSource, /complete: completeAiPreview/);
  assert.match(diagramSource, /rollback: rollbackAiPreview/);
  assert.match(diagramSource, /const targetId = knowledgeDocId \|\| aiEditDraftTargetId/);
  assert.doesNotMatch(
    diagramSource,
    /const openLiveEdit = useCallback\(async[\s\S]*?await saveToKnowledge\(\)/,
    "opening AI Edit must not persist an unsaved Diagram before Accept",
  );
  assert.match(diagramSource, /onAccept=\{acceptAiPreview\}/);
  assert.match(videoSource, /complete: completeVideoAiPreview/);
  assert.match(videoSource, /rollback: rollbackVideoAiPreview/);
  assert.match(videoSource, /commitCoordinator: videoAiCommitCoordinator/);
  assert.match(videoSource, /videoAiCommitCoordinator\.run/);
  assert.match(videoSource, /updateEditorLiveChat\(videoEditorAiEditDetail\)/);
  assert.match(videoSource, /onAccept=\{acceptVideoAiPreview\}/);
  assert.match(fileViewerSource, /pdfAiPreviewRef\.current && !options\.acceptAiPreview/);
  assert.match(fileViewerSource, /imageAiPreviewRef\.current && !options\.acceptAiPreview/);
  assert.match(videoSource, /videoAiPreviewRef\.current && !options\.acceptAiPreview/);
  assert.match(videoSource, /saveRecipe\(\{ acceptAiPreview: true \}\)/);
  assert.match(fileViewerSource, /<AiEditPreviewInteractionShield/);
  assert.match(videoSource, /<AiEditPreviewInteractionShield/);
  assert.match(fileViewerSource, /aiEditInteractionLockProps\(Boolean\(pdfAiPreview\)\)/);
  assert.match(fileViewerSource, /aiEditInteractionLockProps\(Boolean\(imageAiPreview\)\)/);
  assert.match(videoSource, /aiEditInteractionLockProps\(Boolean\(videoAiPreview\)\)/);
  assert.match(controlsSource, /AiEditPreviewInteractionShield/);
  assert.match(controlsSource, /onReview\?: \(\) => void/);
  assert.match(controlsSource, /data-status=\{status\}/);
  assert.match(controlsSource, /AiEditPreviewStatus\.Ready/);
  assert.match(controlsSource, /aria-busy=\{!isReady \|\| accepting \|\| acceptDisabled\}/);
  assert.match(controlsSource, /disabled=\{acceptDisabled \|\| accepting \|\| !isReady\}/);
  assert.match(flowSource, /<AiEditPreviewControls/);
  assert.doesNotMatch(flowSource, /workflow-ai-preview-bar/);
});
