#!/usr/bin/env node
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { build } from "esbuild";

const entryPoint = `
  export {
    fileReferenceKind,
    fileReferenceHref,
    fileReferenceTypeLabel,
    dedupeGeneratedFileRecords,
    generatedFileDocumentId,
    generatedFileFsPath,
    generatedFileIdentity,
    generatedFileLabel,
    generatedFileOpenReference,
    isOpenableFileReference,
    linkifyFileReferencesInMarkdown,
    viewerPathForDocumentId,
  } from "../src/lib/fileReferences.ts";
`;

const bundled = await build({
  stdin: {
    contents: entryPoint,
    loader: "ts",
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
  fileReferenceKind,
  fileReferenceHref,
  fileReferenceTypeLabel,
  dedupeGeneratedFileRecords,
  generatedFileDocumentId,
  generatedFileFsPath,
  generatedFileIdentity,
  generatedFileLabel,
  generatedFileOpenReference,
  isOpenableFileReference,
  linkifyFileReferencesInMarkdown,
  viewerPathForDocumentId,
} = await import(moduleUrl);
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
const taskTimelineSource = await readFile(
  new URL("../src/components/task/TaskExecutionTimeline.tsx", import.meta.url),
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
  assert.equal(viewerPathForDocumentId("doc/id"), "/viewer/doc%2Fid");
  assert.equal(viewerPathForDocumentId(""), null);
  assert.match(inlineCardSource, /navigate\(viewerPath,/);
  assert.doesNotMatch(inlineCardSource, /\|\| docs\[0\]/);
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
    /ArtifactSummaryCards,[\s\S]*ArtifactViewer,[\s\S]*deriveMessageArtifacts/,
  );
  assert.match(workspaceChatSource, /openWorkspaceArtifactDetail/);
  assert.match(workspaceChatSource, /body:\s*<ArtifactViewer artifact=\{artifact\}/);
  assert.match(
    embeddedChatSource,
    /msg\.attachments\?\.forEach\([\s\S]*message-attachment-/,
  );

  const knowledgeLink = `[forecast.xlsx](${viewerPathForDocumentId("01KQDCA7E9E7G20HNYE51VJECQ")})`;
  assert.equal(linkifyFileReferencesInMarkdown(knowledgeLink), knowledgeLink);
  assert.equal(fileReferenceKind("forecast.xlsx"), "spreadsheet");
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
  assert.equal(generatedFileFsPath(filesystemAlias), "Workspaces/Launch/report.md");
  assert.equal(generatedFileFsPath({ fs_path: "documents/brief.pdf" }), "documents/brief.pdf");
  assert.equal(generatedFileOpenReference(document), "/viewer/doc_report");
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

  const legacyBrokenViewer = {
    viewer_url: "/viewer/Workspaces%2FLaunch%2Freport.md",
  };
  assert.equal(
    generatedFileOpenReference(legacyBrokenViewer),
    "Workspaces/Launch/report.md",
  );
  assert.match(fileViewerSource, /generatedFileFsPath\(\{ fs_path: docId \}\)/);
  assert.match(fileViewerSource, /await api\.fs\.read\(legacyFsPath\)/);
});

test("file references expose the matching inline-card kind and type label", () => {
  assert.equal(fileReferenceKind("walkthrough.mp4"), "video");
  assert.equal(fileReferenceKind("voiceover.opus"), "audio");
  assert.equal(fileReferenceKind("cover.avif"), "image");
  assert.equal(fileReferenceKind("forecast.xlsx"), "spreadsheet");
  assert.equal(fileReferenceKind("launch.key"), "presentation");
  assert.equal(fileReferenceKind("recipe.json"), "code");
  assert.equal(fileReferenceKind("bundle.zip"), "archive");
  assert.equal(fileReferenceKind("manual.docx"), "document");
  assert.equal(fileReferenceKind("brief.pdf"), "pdf");
  assert.equal(fileReferenceTypeLabel("walkthrough.mp4"), "MP4");
  assert.equal(fileReferenceTypeLabel("recipe.json"), "JSON");
  assert.equal(fileReferenceTypeLabel("forecast.numbers"), "XLS");
  assert.equal(fileReferenceKind("untitled", "application/pdf"), "pdf");
  assert.equal(fileReferenceTypeLabel("untitled", "application/pdf"), "PDF");
  assert.equal(fileReferenceKind("untitled", undefined, "xlsx"), "spreadsheet");
  assert.match(inlineCardSource, /fileReferenceKind\(resolvedFileName, mimeType, fileType\)/);
  assert.match(inlineCardSource, /<FileTypeIcon kind=\{kind\}/);
  assert.match(inlineCardSource, /inline-file-reference-card__type/);
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
