#!/usr/bin/env node
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { test } from "node:test";
import { build } from "esbuild";

const entryPoint = `
  export {
    fileReferenceHref,
    linkifyFileReferencesInMarkdown,
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

const { fileReferenceHref, linkifyFileReferencesInMarkdown } = await import(moduleUrl);

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

test("inline shell snippets and fenced code remain code", () => {
  assert.equal(
    linkifyFileReferencesInMarkdown("Run `cat file.txt` first."),
    "Run `cat file.txt` first.",
  );
  const fenced = `\`\`\`\n${fsPath}\n\`\`\``;
  assert.equal(linkifyFileReferencesInMarkdown(fenced), fenced);
});
