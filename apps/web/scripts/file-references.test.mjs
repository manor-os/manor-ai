#!/usr/bin/env node
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { test } from "node:test";
import { build } from "esbuild";

const entryPoint = `
  export {
    fileReferenceKind,
    fileReferenceHref,
    fileReferenceTypeLabel,
    dedupeGeneratedFileRecords,
    extractPlatformFileReferences,
    explicitDiagramIdentityOverridesGenericJson,
    filterGeneratedFileRecordsAlreadyLinkedInMarkdown,
    filterGeneratedFileRecordsAlreadyRepresented,
    generatedFileDocumentId,
    generatedFileFsPath,
    generatedFileIdentity,
    generatedFileLabel,
    generatedFileOpenReference,
    isOpenableFileReference,
    linkifyFileReferencesInMarkdown,
    markdownContentWithRenderedAssistantFinalText,
    OfficeEditorFileFactory,
    OfficeEditorFormat,
    officeEditorFileFormat,
    uniqueDocumentForLegacyFilename,
    viewerPathForDocumentId,
  } from "../src/lib/fileReferences.ts";
  export { linkifyChatRouteReferencesInMarkdown } from "../src/lib/chatRouteReferences.ts";
`;

const bundled = await build({
  loader: { ".css": "empty", ".png": "dataurl", ".webp": "dataurl", ".svg": "dataurl" },
  stdin: {
    contents: entryPoint,
    loader: "ts",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "esm",
  platform: "node",
  write: false,
  logLevel: "silent",
});

const moduleUrl = `data:text/javascript;base64,${Buffer.from(
  bundled.outputFiles[0].text,
).toString("base64")}`;

const {
  fileReferenceKind,
  fileReferenceHref,
  fileReferenceTypeLabel,
  dedupeGeneratedFileRecords,
  extractPlatformFileReferences,
  explicitDiagramIdentityOverridesGenericJson,
  filterGeneratedFileRecordsAlreadyLinkedInMarkdown,
  filterGeneratedFileRecordsAlreadyRepresented,
  generatedFileDocumentId,
  generatedFileFsPath,
  generatedFileIdentity,
  generatedFileLabel,
  generatedFileOpenReference,
  isOpenableFileReference,
  linkifyFileReferencesInMarkdown,
  linkifyChatRouteReferencesInMarkdown,
  markdownContentWithRenderedAssistantFinalText,
  OfficeEditorFileFactory,
  OfficeEditorFormat,
  officeEditorFileFormat,
  uniqueDocumentForLegacyFilename,
  viewerPathForDocumentId,
} = await import(moduleUrl);

const chatMessageDisplayBundle = await build({
  loader: { ".css": "empty", ".png": "dataurl", ".webp": "dataurl", ".svg": "dataurl" },
  stdin: {
    contents: `export { parseUserMessageDisplay } from "../src/components/ChatMessageDisplay.tsx";`,
    loader: "ts",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  define: { "import.meta.env": "{}" },
  format: "cjs",
  platform: "node",
  write: false,
  logLevel: "silent",
});
const localStorageDescriptor = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
Object.defineProperty(globalThis, "localStorage", {
  configurable: true,
  value: {
    getItem: () => null,
    removeItem: () => {},
    setItem: () => {},
  },
});
const chatMessageDisplayModule = { exports: {} };
try {
  new Function("require", "module", "exports", chatMessageDisplayBundle.outputFiles[0].text)(
    createRequire(import.meta.url),
    chatMessageDisplayModule,
    chatMessageDisplayModule.exports,
  );
} finally {
  if (localStorageDescriptor) {
    Object.defineProperty(globalThis, "localStorage", localStorageDescriptor);
  } else {
    delete globalThis.localStorage;
  }
}
const { parseUserMessageDisplay } = chatMessageDisplayModule.exports;
const inlineCardSource = await readFile(
  new URL("../src/components/InlineFileReferenceCard.tsx", import.meta.url),
  "utf8",
);
const contentTypeIconsSource = await readFile(
  new URL("../src/components/contentTypeIcons.ts", import.meta.url),
  "utf8",
);
const chatMessageDisplaySource = await readFile(
  new URL("../src/components/ChatMessageDisplay.tsx", import.meta.url),
  "utf8",
);
const chatStreamSource = await readFile(
  new URL("../src/lib/chatStream.ts", import.meta.url),
  "utf8",
);
const videoEditorSource = await readFile(
  new URL("../src/pages/VideoEditor.tsx", import.meta.url),
  "utf8",
);
const taskTimelineSource = await readFile(
  new URL("../src/components/task/TaskExecutionTimeline.tsx", import.meta.url),
  "utf8",
);
const taskLogItemSource = await readFile(
  new URL("../src/components/task/TaskLogItem.tsx", import.meta.url),
  "utf8",
);
const tasksSource = await readFile(
  new URL("../src/pages/Tasks.tsx", import.meta.url),
  "utf8",
);
const taskDetailSource = await readFile(
  new URL("../src/pages/TaskDetail.tsx", import.meta.url),
  "utf8",
);
const embeddedChatSource = await readFile(
  new URL("../src/components/EmbeddedChat.tsx", import.meta.url),
  "utf8",
);
const floatingChatSource = await readFile(
  new URL("../src/components/FloatingChat.tsx", import.meta.url),
  "utf8",
);
const workspaceChatSource = await readFile(
  new URL("../src/components/WorkspaceChat.tsx", import.meta.url),
  "utf8",
);
const fileViewerSource = await readFile(
  new URL("../src/pages/FileViewer.tsx", import.meta.url),
  "utf8",
);

const fsPath = "/api/v1/fs/01KQDCA7E9E7G20HNYE51VJECQ/Robinhood_Infra_Linux_Bash_Staff_Level_Prep.docx";
const fsName = "Robinhood_Infra_Linux_Bash_Staff_Level_Prep.docx";

test("inline API filesystem links become file-reference links", () => {
  assert.equal(
    linkifyFileReferencesInMarkdown(`下载链接：\n\`${fsPath}\``),
    `下载链接：\n[${fsName}](${fileReferenceHref(fsPath)})`,
  );
});

test("plain API filesystem links become file-reference links", () => {
  assert.equal(
    linkifyFileReferencesInMarkdown(`下载链接：${fsPath}`),
    `下载链接：[${fsName}](${fileReferenceHref(fsPath)})`,
  );
});

test("platform file extraction keeps compound diagram JSON paths intact", () => {
  const diagramPath = "/api/v1/fs/entity/Workspaces/Real Estate/manor-workspace-flow.diagram.json";
  const absolutePath = "https://manor.example/api/v1/fs/entity/Reports/flow chart.drawio";
  assert.deepEqual(
    extractPlatformFileReferences(`Saved ${diagramPath}. Open ${absolutePath} when ready.`),
    [diagramPath, absolutePath],
  );
});

test("API filesystem Markdown links with literal spaces become file cards", () => {
  const spacedFsPath = "/api/v1/fs/01KQAW2DS63V54M17Y2B5J4AKB/Workspaces/_by_id/01KXQHHVBGE613PXA0SBP4YP4R/code/creator course intel opc AI workspace file/creator_course_intel_opc_ai.md";
  const content = `[打开 creator_course_intel_opc_ai.md](${spacedFsPath})`;
  assert.equal(
    linkifyFileReferencesInMarkdown(content),
    `[打开 creator_course_intel_opc_ai.md](${fileReferenceHref(spacedFsPath)})`,
  );
  assert.match(inlineCardSource, /api\.fs\.read\(decodedFsPath\)/);
  assert.match(inlineCardSource, /taskOutputPreview:\s*\{/);
  assert.match(fileViewerSource, /resolvedPreview\.encoding === "base64"/);
  assert.match(fileViewerSource, /taskOutputPreviewBlob\(resolvedPreview/);
});

test("Markdown link titles and images are not rewritten as file cards", () => {
  const titledLink = `[Open](${fsPath} "Download the file")`;
  const spacedImage = `![Preview](/api/v1/fs/01KQDCA7E9E7G20HNYE51VJECQ/launch assets/cover.png)`;
  assert.equal(linkifyFileReferencesInMarkdown(titledLink), titledLink);
  assert.equal(linkifyFileReferencesInMarkdown(spacedImage), spacedImage);
});

test("bare file names stay literal even in assistant-style prose and backticks", () => {
  const videoName = "ledgerly-feature-walkthrough.mp4";
  const recipeName = "ledgerly-feature-walkthrough.video-edit-recipe.json";
  const content = `MP4: ${videoName}\nRecipe: \`${recipeName}\``;
  assert.equal(
    linkifyFileReferencesInMarkdown(content),
    content,
  );
});

test("canonical Knowledge Markdown keeps its exact viewer destination", () => {
  const documentId = "01KQDCA7E9E7G20HNYE51VJECQ";
  const fileName = "ledgerly-feature-walkthrough.mp4";
  const content = `[${fileName}](/viewer/${documentId})`;
  assert.equal(
    linkifyFileReferencesInMarkdown(content),
    content,
  );
  assert.equal(isOpenableFileReference(`/viewer/${documentId}`), true);
  assert.equal(isOpenableFileReference(fileName), false);
  assert.equal(viewerPathForDocumentId(documentId), `/viewer/${documentId}`);
  assert.equal(viewerPathForDocumentId("doc/id"), null);
  assert.equal(viewerPathForDocumentId(""), null);
  assert.match(inlineCardSource, /navigate\(viewerPath,/);
  assert.doesNotMatch(inlineCardSource, /\|\| docs\[0\]/);
});

test("structured assistant final Markdown suppresses the duplicate attachment card", () => {
  const linkedDocument = {
    document_id: "01KQDCA7E9E7G20HNYE51VJECQ",
    name: "workspace_development_100_list.docx",
  };
  const linkedContent = markdownContentWithRenderedAssistantFinalText(
    "Processed the Word document.",
    [{
      id: "final-1",
      type: "text",
      phase: "final",
      text: "[workspace_development_100_list.docx](/viewer/01KQDCA7E9E7G20HNYE51VJECQ)",
    }],
  );

  assert.deepEqual(
    filterGeneratedFileRecordsAlreadyLinkedInMarkdown(linkedContent, [linkedDocument]),
    [],
  );
  assert.match(
    chatMessageDisplaySource,
    /filterChatMessageReferencesAlreadyLinked\(\s*renderedChatMessageMarkdownForFileDedupe\(\s*msg,\s*renderedContent/,
  );
});

test("legacy recovered assistant text also suppresses its duplicate attachment card", () => {
  const linkedDocument = {
    document_id: "01KQDCA7E9E7G20HNYE51VJECQ",
    name: "workspace_development_100_list.docx",
  };
  const finalLink = "[workspace_development_100_list.docx](/viewer/01KQDCA7E9E7G20HNYE51VJECQ)";
  const completedBlocks = [
    { id: "process-1", type: "process", status: "completed", steps: [] },
    { id: "legacy-final", type: "text", text: finalLink },
  ];
  const runningBlocks = [
    { id: "process-1", type: "process", status: "running", steps: [] },
    { id: "legacy-final", type: "text", text: finalLink },
  ];

  assert.deepEqual(
    filterGeneratedFileRecordsAlreadyLinkedInMarkdown(
      markdownContentWithRenderedAssistantFinalText("", completedBlocks),
      [linkedDocument],
    ),
    [],
  );
  assert.deepEqual(
    filterGeneratedFileRecordsAlreadyLinkedInMarkdown(
      markdownContentWithRenderedAssistantFinalText("", runningBlocks),
      [linkedDocument],
    ),
    [linkedDocument],
  );
});

test("streaming assistant final text suppresses its duplicate attachment card", () => {
  const linkedDocument = {
    document_id: "01KQDCA7E9E7G20HNYE51VJECQ",
    name: "workspace_development_100_list.docx",
  };
  const finalLink = `[${linkedDocument.name}](/viewer/${linkedDocument.document_id})`;
  const completedBlocks = [
    { id: "process-1", type: "process", status: "completed", steps: [] },
  ];

  assert.equal(
    markdownContentWithRenderedAssistantFinalText(finalLink, undefined, true),
    finalLink,
  );

  assert.equal(
    markdownContentWithRenderedAssistantFinalText(
      finalLink,
      completedBlocks,
      false,
    ),
    "",
  );
  assert.deepEqual(
    filterGeneratedFileRecordsAlreadyLinkedInMarkdown(
      markdownContentWithRenderedAssistantFinalText(
        finalLink,
        completedBlocks,
        true,
      ),
      [linkedDocument],
    ),
    [],
  );
});

test("assistant final Markdown projection does not expose malformed structured blocks", () => {
  assert.equal(
    markdownContentWithRenderedAssistantFinalText("Visible", [
      null,
      { type: "text", text: 42 },
      { type: "unknown", steps: [null, { assistant_text: 42 }] },
    ]),
    "",
  );
});

test("assistant final Markdown projection excludes stale content that is not rendered", () => {
  const linkedDocument = {
    document_id: "01KQDCA7E9E7G20HNYE51VJECQ",
    name: "workspace_development_100_list.docx",
  };
  const staleLink = `[${linkedDocument.name}](/viewer/${linkedDocument.document_id})`;
  const rendered = markdownContentWithRenderedAssistantFinalText(staleLink, [
    { id: "final-visible", type: "text", phase: "final", text: "Done" },
  ]);

  assert.equal(rendered, "Done");
  assert.deepEqual(
    filterGeneratedFileRecordsAlreadyLinkedInMarkdown(rendered, [linkedDocument]),
    [linkedDocument],
  );
});

test("assistant final Markdown projection excludes links hidden by structured formatting", () => {
  const linkedDocument = {
    document_id: "01KQDCA7E9E7G20HNYE51VJECQ",
    name: "workspace_development_100_list.docx",
  };
  const structuredText = JSON.stringify({
    summary: "Done",
    metadata: {
      file: `[${linkedDocument.name}](/viewer/${linkedDocument.document_id})`,
    },
  });
  const rendered = markdownContentWithRenderedAssistantFinalText("", [{
    id: "final-structured",
    type: "text",
    phase: "final",
    text: structuredText,
  }]);

  assert.equal(rendered, "Done");
  assert.deepEqual(
    filterGeneratedFileRecordsAlreadyLinkedInMarkdown(rendered, [linkedDocument]),
    [linkedDocument],
  );
});

test("assistant file dedupe ignores links hidden by display protocol", () => {
  const linkedDocument = {
    document_id: "01KQDCA7E9E7G20HNYE51VJECQ",
    name: "workspace_development_100_list.docx",
  };
  const hiddenLink = `[workspace_development_100_list.docx](/viewer/${linkedDocument.document_id})`;
  const hiddenVariants = [
    `Visible answer<manor-live-patch>{"replacement":${JSON.stringify(hiddenLink)}}</manor-live-patch>`,
    `${hiddenLink}<manor-final-response>Visible answer</manor-final-response>`,
  ];

  hiddenVariants.forEach((text) => {
    const rendered = markdownContentWithRenderedAssistantFinalText(text, [
      { id: "final-hidden", type: "text", phase: "final", text },
    ]);
    assert.equal(rendered.includes(linkedDocument.document_id), false);
    assert.deepEqual(
      filterGeneratedFileRecordsAlreadyLinkedInMarkdown(rendered, [linkedDocument]),
      [linkedDocument],
    );
  });
});

test("hidden file protocol lines do not suppress their reference cards", () => {
  assert.match(
    chatMessageDisplaySource,
    /const renderedContent =\s*options\.renderedContent === undefined\s*\? cleanContent\s*:\s*options\.renderedContent/,
  );
  assert.match(
    chatMessageDisplaySource,
    /renderedChatMessageMarkdownForFileDedupe\(\s*msg,\s*renderedContent,\s*Boolean\(options\.streaming\),?\s*\)/,
  );
  assert.match(
    embeddedChatSource,
    /const renderedBubbleDisplay = parseUserMessageDisplay\([\s\S]*?content:\s*bubbleContent[\s\S]*?const renderedBubbleContent = renderedBubbleDisplay\.cleanContent/,
  );
  assert.match(
    embeddedChatSource,
    /<AssistantMessageBlocks[\s\S]*?content=\{renderedBubbleContent\}/,
  );
  assert.match(
    embeddedChatSource,
    /<ChatMarkdown\s+content=\{renderedBubbleContent\}/,
  );
  assert.match(
    floatingChatSource,
    /const renderedBubbleDisplay = parseUserMessageDisplay\([\s\S]*?content:\s*bubbleContent[\s\S]*?const renderedBubbleContent = renderedBubbleDisplay\.cleanContent/,
  );
  assert.match(
    floatingChatSource,
    /<AssistantMessageBlocks[\s\S]*?content=\{renderedBubbleContent\}/,
  );
  assert.match(
    workspaceChatSource,
    /const renderedBubbleDisplay = parseUserMessageDisplay\([\s\S]*?content:\s*bubbleContent[\s\S]*?const renderedBubbleContent = renderedBubbleDisplay\.cleanContent/,
  );
  assert.match(
    workspaceChatSource,
    /const renderedBodyDisplay = parseUserMessageDisplay\([\s\S]*?content:\s*bodyContent[\s\S]*?const renderedBodyContent = renderedBodyDisplay\.cleanContent/,
  );
  assert.match(
    workspaceChatSource,
    /<AssistantMessageBlocks[\s\S]*?content=\{renderedBubbleContent\}/,
  );
  assert.match(
    workspaceChatSource,
    /<AssistantMessageBlocks[\s\S]*?content=\{renderedBodyContent\}/,
  );
});

test("structured attachment references own the file card before artifact summaries", () => {
  const representedDocument = {
    document_id: "01KQDCA7E9E7G20HNYE51VJECQ",
    name: "workspace_development_100_list.docx",
  };
  const otherDocument = {
    document_id: "01KQDOTHERDOCUMENT00000000000",
    name: representedDocument.name,
  };
  assert.deepEqual(
    filterGeneratedFileRecordsAlreadyRepresented(
      [representedDocument, otherDocument],
      [{ document_id: representedDocument.document_id }],
    ),
    [otherDocument],
  );
  assert.match(
    embeddedChatSource,
    /export function filterMessageArtifactsAlreadyRepresented[\s\S]*?parseUserMessageDisplay\(message, projection\)[\s\S]*?renderedChatMessageMarkdownForFileDedupe\([\s\S]*?filterGeneratedFileRecordsAlreadyLinkedInMarkdown[\s\S]*?parsedDisplay\.references[\s\S]*?filterGeneratedFileRecordsAlreadyRepresented/,
  );
  assert.match(
    embeddedChatSource,
    /keepLatestWorkspaceDraftArtifacts\([\s\S]*?filterMessageArtifactsAlreadyRepresented\(/,
  );
  assert.match(
    workspaceChatSource,
    /filterMessageArtifactsAlreadyRepresented\([\s\S]*?workspaceFileArtifacts\(/,
  );
});

test("artifact dedupe only trusts reference cards within the rendered limit", () => {
  assert.match(
    chatMessageDisplaySource,
    /export const CHAT_MESSAGE_REFERENCE_CARD_LIMIT = 8/,
  );
  assert.match(
    chatMessageDisplaySource,
    /references\.slice\(0, CHAT_MESSAGE_REFERENCE_CARD_LIMIT\)\.map/,
  );
  assert.match(
    embeddedChatSource,
    /parsedDisplay\.references\s*\.slice\(0, CHAT_MESSAGE_REFERENCE_CARD_LIMIT\)\s*\.map/,
  );
});

test("retryable assistant messages dedupe against the rendered error summary", () => {
  assert.match(
    chatMessageDisplaySource,
    /export function renderedChatMessageMarkdownForFileDedupe[\s\S]*?isRetryableAssistantMessage[\s\S]*?displayContentForAssistantMessage[\s\S]*?retryableAssistant \? undefined : msg\.assistant_blocks/,
  );
  assert.match(
    embeddedChatSource,
    /renderedChatMessageMarkdownForFileDedupe\(\s*message,\s*renderedContent,\s*Boolean\(projection\.streaming\),?\s*\)/,
  );
});

test("artifact dedupe follows local coding content overrides", () => {
  assert.match(
    embeddedChatSource,
    /filterMessageArtifactsAlreadyRepresented\([\s\S]*?renderedContent:\s*localCodingNotice \?\? undefined[\s\S]*?streaming/,
  );
  assert.match(
    embeddedChatSource,
    /const messageDisplay = localCodingNotice\s*\? parseUserMessageDisplay\(msg,\s*\{[\s\S]*?renderedContent:\s*renderedBubbleContent[\s\S]*?streaming:\s*isLatestStreaming/,
  );
  assert.match(
    floatingChatSource,
    /const assistantReferenceDisplay = localCodingNotice\s*\? parseUserMessageDisplay\(msg,\s*\{[\s\S]*?renderedContent:\s*renderedBubbleContent[\s\S]*?streaming:\s*isLatestStreaming/,
  );
});

test("hidden assistant bubbles cannot own inline file surfaces", () => {
  assert.match(
    embeddedChatSource,
    /export function assistantMessageRendersInlineFileSurfaces[\s\S]*?credit_exhausted[\s\S]*?isApprovalBoilerplateContent/,
  );
  assert.match(
    embeddedChatSource,
    /filterMessageArtifactsAlreadyRepresented\([\s\S]*?assistantMessageRendersInlineFileSurfaces\(message, streaming\)/,
  );
  assert.match(
    embeddedChatSource,
    /const renderAssistantBubble =\s*msg\.role !== "assistant"[\s\S]*?assistantMessageRendersInlineFileSurfaces\(msg, isLatestStreaming\)/,
  );
});

test("structured chat files already linked in Markdown do not render a second card", () => {
  const linkedDocument = {
    name: "workspace development.docx",
    document_id: "01M0S93GBX7WB7BF7QV9T2WFND",
  };
  const otherDocument = {
    name: "brief.docx",
    document_id: "01M0OTHERDOCUMENT0000000000",
  };
  assert.deepEqual(
    filterGeneratedFileRecordsAlreadyLinkedInMarkdown(
      `[workspace development.docx](/viewer/${linkedDocument.document_id})`,
      [linkedDocument, otherDocument],
    ),
    [otherDocument],
  );
  assert.deepEqual(
    filterGeneratedFileRecordsAlreadyLinkedInMarkdown(
      "[workspace development.docx](</api/v1/fs/entity/Workspaces/workspace development.docx> \"Word file\")",
      [{ name: linkedDocument.name, openUrl: "/api/v1/fs/entity/Workspaces/workspace%20development.docx" }],
    ),
    [],
  );
  assert.deepEqual(
    filterGeneratedFileRecordsAlreadyLinkedInMarkdown(
      "[Report (Final).docx](/api/v1/fs/entity/Workspaces/Report%20(Final).docx)",
      [{
        name: "Report (Final).docx",
        openUrl: "/api/v1/fs/entity/Workspaces/Report%20(Final).docx",
      }],
    ),
    [],
  );
  assert.deepEqual(
    filterGeneratedFileRecordsAlreadyLinkedInMarkdown("No file link", [linkedDocument]),
    [linkedDocument],
  );
});

test("literal filesystem characters survive normalization and URL alias matching", () => {
  const names = ["report#1.md", "report#2.md", "report?2.md", "report%23.md", "report%2520.md", "report%20.md", "report .md", "报告 1.md"];
  const records = names.map(name => ({ name, fsPath: `Workspaces/W/${name}` }));
  assert.equal(dedupeGeneratedFileRecords(records).length, records.length);
  for (const record of records) {
    const address = `/api/v1/fs/entity/${record.fsPath.split("/").map(encodeURIComponent).join("/")}`;
    assert.equal(generatedFileFsPath(record), record.fsPath);
    assert.equal(generatedFileFsPath({ url: address }), record.fsPath);
    assert.equal(generatedFileFsPath({ fsPath: generatedFileFsPath({ url: address }) }), record.fsPath);
    assert.equal(generatedFileOpenReference(record), record.fsPath);
    assert.equal(dedupeGeneratedFileRecords([record, { url: address }]).length, 1);
    assert.deepEqual(filterGeneratedFileRecordsAlreadyLinkedInMarkdown(`[file](${address})`, records), records.filter(item => item !== record));
  }
});

test("shared thumbnails do not merge different documents or claim their file cards", () => {
  for (const previewUrl of ["https://cdn.example/shared-cover.png", "data:image/png;base64,AAAA", "/api/v1/fs/entity/shared-cover.png"]) {
    for (const primary of ["document_id", "fsPath", "openUrl"]) {
      const values = {
        document_id: ["doc_a", "doc_b"], fsPath: ["Workspaces/W/a.pdf", "Workspaces/W/b.pdf"],
        openUrl: ["https://cdn.example/a.pdf", "https://cdn.example/b.pdf"],
      }[primary];
      const records = values.map((value, index) => ({name: `${index ? "b" : "a"}.pdf`, [primary]: value, previewUrl}));
      assert.equal(dedupeGeneratedFileRecords(records).length, 2);
      assert.equal(parseUserMessageDisplay({role: "assistant", content: "Saved files", attachments: records}).references.length, 2);
      assert.deepEqual(filterGeneratedFileRecordsAlreadyRepresented([records[1]], [records[0]]), [records[1]]);
      assert.deepEqual(filterGeneratedFileRecordsAlreadyLinkedInMarkdown(`[preview](${previewUrl})`, records), records);
      for (const record of records) {
        assert.notEqual(generatedFileFsPath(record), "shared-cover.png");
        assert.notEqual(generatedFileOpenReference(record), previewUrl);
      }
      if (primary === "document_id") {
        const display = parseUserMessageDisplay({role: "assistant", content: "[a.pdf](/viewer/doc_a)", attachments: records});
        assert.deepEqual(display.references.map(ref => ref.document_id), ["doc_b"]);
      }
    }
  }
});

test("viewer aliases match local documents but not an external hosts viewer", () => {
  const record = {document_id: "doc_a", name: "a.pdf"};
  assert.deepEqual(filterGeneratedFileRecordsAlreadyLinkedInMarkdown("[a](/viewer/doc_a?download=1#page-2)", [record]), []);
  assert.deepEqual(filterGeneratedFileRecordsAlreadyLinkedInMarkdown("[a](https://other.example/viewer/doc_a)", [record]), [record]);
});

test("attachment projection retains formal file addresses separately from previews", () => {
  for (const field of ["file_url", "document_url"]) {
    const attachments = ["a", "b"].map(name => ({
      name: `${name}.pdf`, document_id: `doc_${name}`,
      [field]: `https://cdn.example/${name}.pdf`, previewUrl: "https://cdn.example/shared-cover.png",
    }));
    const display = parseUserMessageDisplay({role: "assistant", content: "[a.pdf](https://cdn.example/a.pdf)", attachments});
    assert.deepEqual(display.references.map(ref => ref.document_id), ["doc_b"]);
  }
});

test("completion previewUrl attachments are owned by the inline Markdown card", () => {
  const name = "AI_SDE_20小时课程_学生教材版.md";
  const address = `/api/v1/fs/entity/Workspaces/Demo/${encodeURIComponent(name)}`;
  const content = `**Files and outputs saved**\n- File: [${name}](${address})`;
  for (const field of ["previewUrl", "preview_url", "file_url", "openUrl", "url"]) {
    const attachment = { name, fileType: "md", [field]: address };
    assert.equal(generatedFileFsPath(attachment), `Workspaces/Demo/${name}`);
    assert.equal(generatedFileOpenReference(attachment), address);
    assert.deepEqual(filterGeneratedFileRecordsAlreadyLinkedInMarkdown(content, [attachment]), []);
    const display = parseUserMessageDisplay({ role: "assistant", content, attachments: [attachment] });
    assert.equal(display.references.length, 0, `${field} must not render an extra card`);
    assert.equal(display.cleanContent, content);
  }
});

test("preview-only attachments retain distinct paths and survive without an inline link", () => {
  const first = { name: "report.md", previewUrl: "/api/v1/fs/entity/Workspaces/A/report.md" };
  const second = { name: "report.md", previewUrl: "/api/v1/fs/entity/Workspaces/B/report.md" };
  const uppercase = { name: "REPORT.md", previewUrl: "/api/v1/fs/entity/Workspaces/A/REPORT.md" };
  const records = [first, second, uppercase];
  assert.equal(dedupeGeneratedFileRecords(records).length, 3);
  const display = parseUserMessageDisplay({
    role: "assistant", content: `[report.md](${first.previewUrl})`, attachments: records,
  });
  assert.deepEqual(display.references.map((ref) => ref.previewUrl), [second.previewUrl, uppercase.previewUrl]);
  assert.equal(parseUserMessageDisplay({ role: "assistant", content: "Saved files", attachments: records }).references.length, 3);
  assert.equal(dedupeGeneratedFileRecords([first, { ...first, preview_url: first.previewUrl }]).length, 1);
});

test("an attachment and its hidden file protocol line produce one canonical card", () => {
  const documentId = "01M0S93GBX7WB7BF7QV9T2WFND";
  const filename = "workspace_development_100_list.docx";
  const display = parseUserMessageDisplay({
    id: "message-with-duplicate-file-metadata",
    role: "user",
    content: `已整理成 Word 文档：\n[File: ${filename} -> /viewer/${documentId}]`,
    attachments: [{ name: filename, document_id: documentId }],
  });

  assert.equal(display.cleanContent, "已整理成 Word 文档：");
  assert.deepEqual(
    display.references.map(({ key, name, document_id, url }) => ({
      key,
      name,
      document_id,
      url,
    })),
    [{ key: documentId, name: filename, document_id: documentId, url: undefined }],
  );
});

test("bare viewer routes follow the same file-card projection as ChatMarkdown", () => {
  const linkedDocument = {
    name: "workspace development.docx",
    document_id: "01M0S93GBX7WB7BF7QV9T2WFND",
  };

  assert.deepEqual(
    filterGeneratedFileRecordsAlreadyLinkedInMarkdown(
      `已整理：/viewer/${linkedDocument.document_id}`,
      [linkedDocument],
    ),
    [],
  );
});

test("structured chat files already linked by Markdown references do not render twice", () => {
  const linkedDocument = {
    name: "workspace development.docx",
    document_id: "01M0S93GBX7WB7BF7QV9T2WFND",
    fsPath: "Workspaces/workspace development.docx",
  };
  const destination = `/viewer/${linkedDocument.document_id}`;
  for (const content of [
    `[workspace development.docx][download]\n\n[download]: ${destination}`,
    `[workspace development.docx][]\n\n[workspace development.docx]: ${destination}`,
    `[workspace development.docx]\n\n[workspace development.docx]: ${destination}`,
    `[workspace development.docx][download]\n\n[download]: </api/v1/fs/entity/Workspaces/workspace%20development.docx>`,
  ]) {
    assert.deepEqual(
      filterGeneratedFileRecordsAlreadyLinkedInMarkdown(content, [linkedDocument]),
      [],
    );
  }
  assert.deepEqual(
    filterGeneratedFileRecordsAlreadyLinkedInMarkdown(
      `[download]: ${destination}`,
      [linkedDocument],
    ),
    [linkedDocument],
  );
});

test("the first duplicate Markdown definition owns the rendered reference link", () => {
  const firstDocument = {
    name: "first.docx",
    document_id: "01M0FIRSTDOCUMENT00000000000",
  };
  const secondDocument = {
    name: "second.docx",
    document_id: "01M0SECONDDOCUMENT0000000000",
  };
  const content = [
    "[Open document][download]",
    "",
    `[download]: /viewer/${firstDocument.document_id}`,
    `[download]: /viewer/${secondDocument.document_id}`,
  ].join("\n");

  assert.deepEqual(
    filterGeneratedFileRecordsAlreadyLinkedInMarkdown(
      content,
      [firstDocument, secondDocument],
    ),
    [secondDocument],
  );
});

test("route linkification protects only definitions that CommonMark actually parses", () => {
  const documentId = "01M0S93GBX7WB7BF7QV9T2WFND";
  const visibleProse = `[note]: see /viewer/${documentId}`;
  assert.equal(
    linkifyChatRouteReferencesInMarkdown(visibleProse),
    `[note]: see [File T2WFND](manor-route:%2Fviewer%2F${documentId})`,
  );

  const multilineDefinition = [
    "[Open document][download]",
    "",
    "[download]:",
    `  /viewer/${documentId}`,
  ].join("\n");
  assert.equal(
    linkifyChatRouteReferencesInMarkdown(multilineDefinition),
    multilineDefinition,
  );

  for (const nestedDefinition of [
    `> [download]: /viewer/${documentId}\n> [Open document][download]`,
    `- [download]: /viewer/${documentId}\n  [Open document][download]`,
  ]) {
    assert.equal(
      linkifyChatRouteReferencesInMarkdown(nestedDefinition),
      nestedDefinition,
    );
  }
});

test("Markdown images do not claim the structured file card", () => {
  const linkedDocument = {
    name: "preview.png",
    document_id: "01M0S93GBX7WB7BF7QV9T2WFND",
  };
  const destination = `/viewer/${linkedDocument.document_id}`;

  for (const content of [
    `![Preview](${destination})`,
    `![Preview][asset]\n\n[asset]: ${destination}`,
  ]) {
    assert.deepEqual(
      filterGeneratedFileRecordsAlreadyLinkedInMarkdown(content, [linkedDocument]),
      [linkedDocument],
    );
  }
});

test("GFM bare file URLs claim the duplicate structured file card", () => {
  const destination = "https://cdn.example.com/report.pdf";
  const linkedFile = {
    name: "report.pdf",
    openUrl: destination,
  };

  assert.deepEqual(
    filterGeneratedFileRecordsAlreadyLinkedInMarkdown(destination, [linkedFile]),
    [],
  );
});

test("structured file dedupe ignores code examples and escaped Markdown links", () => {
  const linkedDocument = {
    name: "workspace development.docx",
    document_id: "01M0S93GBX7WB7BF7QV9T2WFND",
  };
  const destination = `/viewer/${linkedDocument.document_id}`;
  for (const content of [
    `\`[workspace development.docx](${destination})\``,
    `\`\`\`md\n[workspace development.docx](${destination})\n\`\`\``,
    `    [workspace development.docx](${destination})`,
    `<!-- [workspace development.docx](${destination}) -->`,
    `<div>\n[workspace development.docx](${destination})\n</div>`,
    `<pre>\n[workspace development.docx](${destination})\n</pre>`,
    `<script>\n\n[workspace development.docx](${destination})\n\n</script>`,
    `<custom-card>\n[workspace development.docx](${destination})\n</custom-card>`,
    `<?hidden\n[workspace development.docx](${destination})\n?>`,
    `<![CDATA[\n[workspace development.docx](${destination})\n]]>`,
    `\\[workspace development.docx](${destination})`,
  ]) {
    assert.deepEqual(
      filterGeneratedFileRecordsAlreadyLinkedInMarkdown(content, [linkedDocument]),
      [linkedDocument],
    );
  }
});

test("HTML-looking fenced code does not hide a following rendered file link", () => {
  const linkedDocument = {
    name: "workspace development.docx",
    document_id: "01M0S93GBX7WB7BF7QV9T2WFND",
  };
  const destination = `/viewer/${linkedDocument.document_id}`;
  const content = [
    "```html",
    "<div>",
    "```",
    "",
    `[workspace development.docx](${destination})`,
  ].join("\n");

  assert.deepEqual(
    filterGeneratedFileRecordsAlreadyLinkedInMarkdown(content, [linkedDocument]),
    [],
  );
});

test("unclosed fenced code cannot own a structured file card", () => {
  const linkedDocument = {
    name: "workspace development.docx",
    document_id: "01M0S93GBX7WB7BF7QV9T2WFND",
  };
  const destination = `/viewer/${linkedDocument.document_id}`;
  const content = [
    "Visible introduction",
    "",
    "```md",
    `[workspace development.docx](${destination})`,
  ].join("\n");

  assert.deepEqual(
    filterGeneratedFileRecordsAlreadyLinkedInMarkdown(content, [linkedDocument]),
    [linkedDocument],
  );
});

test("raw HTML containers take precedence over nested Markdown fences", () => {
  const linkedDocument = {
    name: "workspace development.docx",
    document_id: "01M0S93GBX7WB7BF7QV9T2WFND",
  };
  const destination = `/viewer/${linkedDocument.document_id}`;
  const content = [
    "<script>",
    "```md",
    "example",
    "```",
    `[workspace development.docx](${destination})`,
    "</script>",
  ].join("\n");

  assert.deepEqual(
    filterGeneratedFileRecordsAlreadyLinkedInMarkdown(content, [linkedDocument]),
    [linkedDocument],
  );
});

test("type-seven HTML inside a paragraph does not hide rendered file links", () => {
  const linkedDocument = {
    name: "workspace development.docx",
    document_id: "01M0S93GBX7WB7BF7QV9T2WFND",
  };
  const destination = `/viewer/${linkedDocument.document_id}`;
  const content = [
    "Paragraph text",
    "<custom-card>",
    `[workspace development.docx](${destination})`,
    "</custom-card>",
  ].join("\n");

  assert.deepEqual(
    filterGeneratedFileRecordsAlreadyLinkedInMarkdown(content, [linkedDocument]),
    [],
  );
});

test("structured file dedupe recognizes viewer and filesystem aliases of one file", () => {
  const linkedDocument = {
    name: "workspace development.docx",
    document_id: "01M0S93GBX7WB7BF7QV9T2WFND",
    fsPath: "Workspaces/workspace development.docx",
  };
  assert.deepEqual(
    filterGeneratedFileRecordsAlreadyLinkedInMarkdown(
      "[workspace development.docx](/api/v1/fs/entity/Workspaces/workspace%20development.docx)",
      [linkedDocument],
    ),
    [],
  );
  assert.deepEqual(
    filterGeneratedFileRecordsAlreadyLinkedInMarkdown(
      "[workspace development.docx](/viewer/a-different-document)",
      [linkedDocument],
    ),
    [linkedDocument],
  );
  assert.deepEqual(
    filterGeneratedFileRecordsAlreadyLinkedInMarkdown(
      "`/api/v1/fs/entity/Workspaces/workspace development.docx`",
      [linkedDocument],
    ),
    [],
  );
});

test("task, generated, and Knowledge files share typed cards with exact viewer routes", () => {
  assert.match(taskTimelineSource, /<InlineFileReferenceCard/);
  assert.match(taskTimelineSource, /generatedFileOpenReference\(file\)/);
  assert.match(taskTimelineSource, /files\.length \? "" : compactStructuredPreview\(result\)/);
  assert.doesNotMatch(taskTimelineSource, /target="_blank"/);
  assert.match(tasksSource, /generatedFileOpenReference\(file\)/);
  assert.match(tasksSource, /dedupeGeneratedFileRecords\(\[\.\.\.files, \.\.\.stepFiles\]\)/);
  assert.match(tasksSource, /trustedReference/);
  assert.match(taskDetailSource, /<InlineFileReferenceCard/);
  assert.match(taskDetailSource, /navigationState=\{viewerState\}/);
  assert.match(taskDetailSource, /fileType=\{f\.file_type \|\| f\.fileType\}/);
  assert.doesNotMatch(taskDetailSource, /taskOutputErrorMessage\s*\|\|\s*formatUserFacingStructuredText\(taskOutput\)/);

  assert.match(chatMessageDisplaySource, /generatedFileOpenReference\(\{/);
  assert.match(chatMessageDisplaySource, /open_url:\s*refItem\.openUrl/);
  assert.match(chatMessageDisplaySource, /<InlineFileReferenceCard/);
  assert.doesNotMatch(chatMessageDisplaySource, /kindMatches\[0\]/);
  assert.doesNotMatch(chatMessageDisplaySource, /items\[0\]/);
  assert.match(embeddedChatSource, /inlineFileCards/);
  assert.match(floatingChatSource, /inlineFileCards/);
  assert.match(workspaceChatSource, /chatMessageReferencesFromAttachments/);
  assert.match(workspaceChatSource, /inlineFileCards/);
  assert.match(
    workspaceChatSource,
    /ArtifactSummaryCards,[\s\S]*OutputPanel,[\s\S]*deriveMessageArtifacts/,
  );
  assert.match(workspaceChatSource, /onArtifactOpen=\{openWorkspaceArtifact\}/);
  assert.doesNotMatch(workspaceChatSource, /openWorkspaceArtifactDetail/);
  assert.match(
    embeddedChatSource,
    /msg\.attachments\?\.forEach\([\s\S]*message-attachment-/,
  );

  const knowledgeLink = `[forecast.xlsx](${viewerPathForDocumentId("01KQDCA7E9E7G20HNYE51VJECQ")})`;
  assert.equal(linkifyFileReferencesInMarkdown(knowledgeLink), knowledgeLink);
  assert.equal(fileReferenceKind("forecast.xlsx"), "spreadsheet");
});

test("viewer back restores the route and exact file source position", () => {
  assert.match(fileViewerSource, /window\.history\.state\?\.idx/);
  assert.match(fileViewerSource, /navigate\(-1\)/);
  assert.match(fileViewerSource, /navigate\(viewerReturnTo \|\| "\/knowledge", \{ replace: true \}\)/);
  assert.match(inlineCardSource, /sourceAnchorId\?: string/);
  assert.match(inlineCardSource, /source\.scrollIntoView\(\{ block: "center", inline: "nearest" \}\)/);
  assert.match(tasksSource, /searchParams\.get\("task"\)/);
  assert.match(tasksSource, /next\.set\("task", task\.id\)/);
  assert.match(tasksSource, /sourceAnchorId=\{taskOutputFileAnchorId\(task\.id, identity, i\)\}/);
  assert.match(taskLogItemSource, /returnTo=\{sourceReturnTo\}/);
  assert.match(taskLogItemSource, /rowRef\.current\?\.scrollIntoView/);
});

test("file cards share the canonical mode icons for matching content types", () => {
  assert.match(inlineCardSource, /getFileReferenceIcon\(kind\)/);
  assert.match(contentTypeIconsSource, /presentation: CONTENT_TYPE_ICONS\.slides/);
  assert.match(contentTypeIconsSource, /pdf: CONTENT_TYPE_ICONS\.pdf/);
  assert.match(contentTypeIconsSource, /spreadsheet: CONTENT_TYPE_ICONS\.sheet/);
  assert.match(contentTypeIconsSource, /page: CONTENT_TYPE_ICONS\.website/);
  assert.match(contentTypeIconsSource, /image: CONTENT_TYPE_ICONS\.image/);
  assert.match(contentTypeIconsSource, /video: CONTENT_TYPE_ICONS\.video/);
  assert.match(contentTypeIconsSource, /audio: CONTENT_TYPE_ICONS\.audio/);
  assert.match(contentTypeIconsSource, /document: CONTENT_TYPE_ICONS\.document/);
});

test("generated file records resolve one exact address and merge aliases", () => {
  const document = {
    document_id: "doc_report",
    fs_path: "Workspaces/Launch/report.md",
    open_url: "/viewer/doc_report",
    name: "report.md",
  };
  const filesystemAlias = {
    type: "file",
    url: "/api/v1/fs/entity/Workspaces/Launch/report.md",
  };
  assert.equal(generatedFileDocumentId(document), "doc_report");
  assert.equal(generatedFileDocumentId({ documentId: "doc_alias" }), "");
  assert.equal(generatedFileDocumentId({ doc_id: "doc_alias" }), "");
  assert.equal(generatedFileDocumentId({ id: "doc_alias", mime_type: "text/plain" }), "");
  assert.equal(generatedFileFsPath(filesystemAlias), "Workspaces/Launch/report.md");
  assert.equal(generatedFileFsPath({ fs_path: "documents/brief.pdf" }), "documents/brief.pdf");
  assert.equal(generatedFileOpenReference(document), "/viewer/doc_report");
  assert.equal(
    generatedFileOpenReference({
      document_id: "doc_exact",
      open_url: "/viewer/doc_wrong",
      fs_path: "Workspaces/Launch/report.md",
    }),
    "/viewer/doc_exact",
  );
  assert.equal(generatedFileOpenReference(filesystemAlias), filesystemAlias.url);
  assert.equal(generatedFileLabel(document), "report.md");
  assert.equal(generatedFileIdentity(document), "document:doc_report");
  assert.deepEqual(dedupeGeneratedFileRecords([document, filesystemAlias]), [
    { ...document, type: "file", url: filesystemAlias.url },
  ]);
  const bridged = dedupeGeneratedFileRecords([
    { document_id: "doc_report", name: "report.md" },
    { fs_path: "Workspaces/Launch/report.md" },
    document,
  ]);
  assert.equal(bridged.length, 1);
  assert.equal(bridged[0].document_id, "doc_report");
  assert.equal(bridged[0].fs_path, "Workspaces/Launch/report.md");

  const legacyPathViewer = {
    viewer_url: "/viewer/Workspaces%2FLaunch%2Freport.md",
  };
  assert.equal(
    generatedFileOpenReference(legacyPathViewer),
    "Workspaces/Launch/report.md",
  );
  assert.equal(
    generatedFileOpenReference({ viewer_url: "/viewer/doc_report" }),
    "/viewer/doc_report",
  );
  assert.doesNotMatch(fileViewerSource, /generatedFileFsPath\(\{ fs_path: docId \}\)/);
  assert.doesNotMatch(fileViewerSource, /await api\.fs\.read\(legacyFsPath\)/);
});

test("document previews accept only the canonical document_id", () => {
  const attachmentParser = chatMessageDisplaySource.match(
    /export function chatMessageReferencesFromAttachments[\s\S]*?function documentReferenceKind/,
  )?.[0];
  assert.ok(attachmentParser);
  assert.match(attachmentParser, /attachment\.document_id/);
  assert.doesNotMatch(attachmentParser, /attachment\.id/);
  assert.doesNotMatch(attachmentParser, /attachment\.documentId/);
  assert.doesNotMatch(attachmentParser, /attachment\.doc_id/);
  assert.doesNotMatch(fileViewerSource, /api\.documents\.listAll\(\{ search: docId/);
  assert.doesNotMatch(fileViewerSource, /uniqueDocumentForLegacyFilename/);
  assert.match(chatMessageDisplaySource, /api\.documents\.get\(documentId\)/);
  assert.doesNotMatch(chatMessageDisplaySource, /api\.documents\.list\(/);

  const streamAttachmentNormalizer = chatStreamSource.match(
    /function normalizeMessageAttachments[\s\S]*?function normalizeToolArguments/,
  )?.[0];
  assert.ok(streamAttachmentNormalizer);
  assert.match(streamAttachmentNormalizer, /item\.document_id/);
  assert.doesNotMatch(streamAttachmentNormalizer, /item\.id/);

  const recipeMatcher = videoEditorSource.match(
    /function recipeReferencesDocument[\s\S]*?function recipeSearchFolderIds/,
  )?.[0];
  assert.ok(recipeMatcher);
  assert.match(recipeMatcher, /item\.document_id/);
  assert.doesNotMatch(recipeMatcher, /item\.doc_id/);
  assert.doesNotMatch(recipeMatcher, /item\.asset_document_id/);
  assert.doesNotMatch(recipeMatcher, /item\.assetDocumentId/);
  assert.doesNotMatch(recipeMatcher, /normalizeRecipePath/);
  assert.doesNotMatch(recipeMatcher, /recipePathBaseName/);
  assert.doesNotMatch(recipeMatcher, /final_video_path/);
  assert.doesNotMatch(recipeMatcher, /clean_picture_master/);
});

test("chat artifacts preserve exact document identity", () => {
  const dedupSource = embeddedChatSource.match(
    /function artifactDedupKey[\s\S]*?function dedupeArtifacts/,
  )?.[0];
  assert.ok(dedupSource);
  assert.match(dedupSource, /artifact\.data\?\.document_id/);
  assert.ok(
    dedupSource.indexOf("document_id") < dedupSource.indexOf("const fileLike"),
  );

  const messageArtifactsSource = embeddedChatSource.match(
    /export function deriveMessageArtifacts[\s\S]*?export function keepLatestWorkspaceDraftArtifacts/,
  )?.[0];
  assert.ok(messageArtifactsSource);
  assert.match(messageArtifactsSource, /generatedFileOpenReference\(\{/);
  assert.match(messageArtifactsSource, /document_id: documentId/);

  const messageReferenceArtifactSource = embeddedChatSource.match(
    /function artifactFromMessageReference[\s\S]*?function buildMessageInlineParts/,
  )?.[0];
  assert.ok(messageReferenceArtifactSource);
  assert.match(messageReferenceArtifactSource, /generatedFileOpenReference\(\{/);
  assert.match(messageReferenceArtifactSource, /document_id: documentId/);

  const toolArtifactSource = embeddedChatSource.match(
    /function artifactFromRecord[\s\S]*?function structuredArtifactsFromResult/,
  )?.[0];
  assert.ok(toolArtifactSource);
  assert.match(toolArtifactSource, /const openHref = generatedFileOpenReference\(\{/);
  assert.match(toolArtifactSource, /document_id: documentId/);
});

test("file references expose the matching inline-card kind and type label", () => {
  assert.equal(fileReferenceKind("walkthrough.mp4"), "video");
  assert.equal(fileReferenceKind("voiceover.opus"), "audio");
  assert.equal(fileReferenceKind("cover.avif"), "image");
  assert.equal(fileReferenceKind("forecast.xlsx"), "spreadsheet");
  assert.equal(fileReferenceKind("launch.key"), "presentation");
  assert.equal(fileReferenceKind("recipe.json"), "code");
  assert.equal(fileReferenceKind("workspace-flow.diagram.json", "application/json"), "diagram");
  assert.equal(fileReferenceKind("workspace-flow.mmd", "text/plain"), "diagram");
  assert.equal(fileReferenceKind("workspace-flow.mermaid", "text/plain"), "diagram");
  assert.equal(fileReferenceKind("workspace-flow.drawio", "application/xml"), "diagram");
  assert.equal(fileReferenceKind("workspace-flow.json", "application/json", "diagram"), "diagram");
  assert.equal(fileReferenceKind("workspace-flow.json", "application/json", "diagram.json"), "diagram");
  assert.equal(fileReferenceKind("renamed-flow.mmd", "application/json", "json"), "code");
  assert.equal(
    fileReferenceKind(
      "renamed-deck.drawio",
      "application/vnd.openxmlformats-officedocument.presentationml.presentation",
      "pptx",
    ),
    "presentation",
  );
  assert.equal(
    fileReferenceKind("legacy-flow.diagram.json", "application/json", "json"),
    "diagram",
  );
  assert.equal(fileReferenceKind("bundle.zip"), "archive");
  assert.equal(fileReferenceKind("manual.docx"), "document");
  assert.equal(fileReferenceKind("brief.pdf"), "pdf");
  assert.equal(fileReferenceTypeLabel("walkthrough.mp4"), "MP4");
  assert.equal(fileReferenceTypeLabel("recipe.json"), "JSON");
  assert.equal(fileReferenceTypeLabel("workspace-flow.diagram.json", "application/json"), "DIA");
  assert.equal(fileReferenceTypeLabel("workspace-flow.json", "application/json", "diagram"), "DIA");
  assert.equal(fileReferenceTypeLabel("forecast.numbers"), "XLS");
  assert.equal(fileReferenceKind("untitled", "application/pdf"), "pdf");
  assert.equal(fileReferenceTypeLabel("untitled", "application/pdf"), "PDF");
  assert.equal(fileReferenceKind("untitled", undefined, "xlsx"), "spreadsheet");
  assert.match(inlineCardSource, /fileReferenceKind\(resolvedFileName, mimeType, fileType\)/);
  assert.match(inlineCardSource, /<FileTypeIcon kind=\{kind\}/);
  assert.match(inlineCardSource, /inline-file-reference-card__type/);
});

test("Office editor format follows durable type after a rename", () => {
  assert.equal(
    officeEditorFileFormat("Renamed deck.json", "application/json", "pptx"),
    "pptx",
  );
  assert.equal(
    officeEditorFileFormat("Renamed workbook.json", "application/json", "xlsx"),
    "xlsx",
  );
  assert.equal(
    officeEditorFileFormat("Renamed brief.json", "application/json", "docx"),
    "docx",
  );
  assert.equal(
    officeEditorFileFormat(
      "Quarterly deck.pptx",
      "application/vnd.openxmlformats-officedocument.presentationml.presentation",
      "presentation",
    ),
    "pptx",
  );
  assert.equal(
    officeEditorFileFormat("Misleading deck.pptx", "application/json", "json"),
    "",
  );
  assert.equal(
    officeEditorFileFormat("Misleading brief.docx", "application/msword", "txt"),
    "",
  );
  assert.equal(officeEditorFileFormat("Legacy brief.wps"), "doc");
  assert.equal(officeEditorFileFormat("Legacy workbook.et"), "xls");
  assert.equal(officeEditorFileFormat("Legacy deck.dps"), "ppt");
  assert.equal(OfficeEditorFormat.Ppt, "ppt");
  assert.deepEqual(
    OfficeEditorFileFactory.create("Renamed deck.json", "application/json", "ppt"),
    {
      format: OfficeEditorFormat.Ppt,
      kind: "presentation",
      requiresLegacyConversion: true,
      usesBinaryPackage: true,
    },
  );
  assert.equal(
    OfficeEditorFileFactory.create("Renamed workbook.json", "application/json", "et").requiresLegacyConversion,
    true,
  );
  assert.equal(fileReferenceKind("Legacy brief.wps"), "document");
  assert.equal(fileReferenceKind("Legacy workbook.et"), "spreadsheet");
  assert.equal(fileReferenceKind("Legacy deck.dps"), "presentation");
});

test("explicit diagram identity overrides legacy generic JSON metadata", () => {
  assert.equal(
    explicitDiagramIdentityOverridesGenericJson("json", "diagram"),
    true,
  );
  assert.equal(
    explicitDiagramIdentityOverridesGenericJson("json", "code"),
    false,
  );
  assert.equal(
    explicitDiagramIdentityOverridesGenericJson("diagram.json", "diagram"),
    false,
  );
});

test("a markdown link whose target is an fs path becomes a file-reference link", () => {
  assert.equal(
    linkifyFileReferencesInMarkdown(`Open it here: [${fsPath}](${fsPath})`),
    `Open it here: [${fsName}](${fileReferenceHref(fsPath)})`,
  );
});

test("a space in the fs path no longer leaves a raw markdown link in the text", () => {
  // The agent echoes the tool's entry_url verbatim; a bundle directory with a
  // space is not a legal CommonMark destination, so the renderer used to print
  // the whole internal path twice.
  const spaced =
    "/api/v1/fs/01KQDCA7E9E7G20HNYE51VJECQ/code/travelling maker landing/index.html";
  assert.equal(
    linkifyFileReferencesInMarkdown(`HTML: [${spaced}](${spaced})`),
    `HTML: [index.html](${fileReferenceHref(spaced)})`,
  );
});

test("a label the agent actually wrote survives the rewrite", () => {
  assert.equal(
    linkifyFileReferencesInMarkdown(`[打开预览](${fsPath})`),
    `[打开预览](${fileReferenceHref(fsPath)})`,
  );
});

test("images and non-fs links are left alone", () => {
  const image = `![cover](${fsPath})`;
  assert.equal(linkifyFileReferencesInMarkdown(image), image);
  const external = "[docs](https://example.com/guide.pdf)";
  assert.equal(linkifyFileReferencesInMarkdown(external), external);
});

test("inline shell snippets and fenced code remain code", () => {
  assert.equal(
    linkifyFileReferencesInMarkdown("Run `cat file.txt` first."),
    "Run `cat file.txt` first.",
  );
  const fenced = `\`\`\`\n${fsPath}\n\`\`\``;
  assert.equal(linkifyFileReferencesInMarkdown(fenced), fenced);
});
