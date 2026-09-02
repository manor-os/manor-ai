import assert from "node:assert/strict";
import test from "node:test";
import { build } from "esbuild";
import { fromMarkdown } from "mdast-util-from-markdown";

const bundle = await build({
  entryPoints: [new URL("../src/lib/remarkSourceCitations.mjs", import.meta.url).pathname],
  bundle: true, platform: "node", format: "esm", write: false,
});
const { default: plugin } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`);
const project = (text) => {
  const tree = fromMarkdown(text);
  plugin()(tree);
  return tree;
};
const all = (node, type) => [
  ...(node.type === type ? [node] : []), ...(node.children || []).flatMap((child) => all(child, type)),
];
const summaries = (text) => all(project(text), "sourceCitation").map((node) => node.data.hProperties);

test("named inline sources expose their actual title and site icon without changing the URL", () => {
  const text = '正文 [MDN 官方文档](https://developer.mozilla.org/en-US/docs/Web?x=1#intro "source")。';
  assert.deepEqual(summaries(text)[0], {
    "data-source-citation": true,
    "data-source-label": "MDN 官方文档",
    "data-source-kind": "web",
    "data-source-icon": "https://developer.mozilla.org/favicon.ico",
    "data-source-count": 1,
  });
  assert.equal(all(project(text), "link")[0].url, "https://developer.mozilla.org/en-US/docs/Web?x=1#intro");
  assert.equal(summaries('[文档][ref]\n\n[ref]: https://example.test/docs "source"')[0]["data-source-label"], "文档");
});

test("generic citations use a hostname, file evidence uses its label, grouped sources keep a count", () => {
  assert.equal(summaries("正文 [Source](https://www.example.test/docs)")[0]["data-source-label"], "example.test");
  assert.equal(summaries("正文 [1](http://example.test/docs)")[0]["data-source-icon"], "");
  assert.equal(summaries("CREDIT: Based on `面试笔记.md`.")[0]["data-source-label"], "面试笔记.md");
  const file = summaries('正文 [面试笔记.md](/viewer/document-id "source")')[0];
  assert.equal(file["data-source-label"], "面试笔记.md");
  assert.equal(file["data-source-kind"], "file");
  const grouped = summaries("Sources:\n\n- [官方文档](https://example.test/docs)\n- [同一来源](https://example.test/docs)\n- [笔记](/viewer/document-id)")[0];
  assert.equal(grouped["data-source-count"], 2);
});

test("legacy CREDIT line collapses without hiding adjacent answer text or inventing a file link", () => {
  const tree = project("开始回答。\nCREDIT: Based on `Pinterest_Adobe_LeetCode_面试真题整理.md`, Minimum Window Substring.\n继续提问。");
  assert.equal(all(tree, "sourceCitation").length, 1);
  assert.equal(all(tree, "link").length, 0);
  assert.match(JSON.stringify(all(tree, "sourceCitation")[0]), /Minimum Window Substring/);
  assert.match(JSON.stringify(tree), /开始回答/);
  assert.match(JSON.stringify(tree), /继续提问/);
});

test("file and web Sources lists share one disclosure and keep exact destinations", () => {
  for (const heading of ["Sources:", "### Sources", "**来源：**", "References:"]) {
    const tree = project(`${heading}\n\n- [面试笔记](/viewer/document-id)\n- [Web](https://example.test/a?q=x#section)\n\nNext paragraph.`);
    assert.equal(all(tree, "sourceCitation").length, 1, heading);
    assert.deepEqual(all(tree, "link").map((node) => node.url), ["/viewer/document-id", "https://example.test/a?q=x#section"]);
    assert.equal(tree.children.at(-1).children[0].value, "Next paragraph.");
  }
});

test("plain-line source groups and Markdown reference definitions survive projection", () => {
  const tree = project("Sources:\n[Official docs][docs]\n[Uploaded file](/viewer/abc)\n\n[docs]: https://example.test/docs");
  assert.equal(all(tree, "sourceCitation").length, 1);
  assert.equal(all(tree, "linkReference")[0].identifier, "docs");
  assert.equal(all(tree, "definition")[0].url, "https://example.test/docs");
});

test("inline and list-item web citations collapse while ordinary navigation stays unchanged", () => {
  const tree = project("A claim [Source](https://example.test/one). Another ([Research](https://example.test/two)).\n\n- Fact [1](https://example.test/three)\n- Visit [website](https://example.test/home) or ([Settings](/settings)).");
  assert.equal(all(tree, "sourceCitation").length, 3);
  assert.equal(all(tree, "link").length, 5);
});

test("code, quoted examples, billing credits, incomplete streams and invalid URL schemes stay unchanged", () => {
  for (const text of [
    '```text\nCREDIT: `report.md`\n```',
    '> CREDIT: `report.md`',
    'Credits: 20 remaining',
    'Sources:',
    '[Source](javascript:alert%281%29)',
    '[Source](data:text/html,hello)',
    '[Source](/settings)',
    '[文档](javascript:alert%281%29 "source")',
    '[文档](data:text/html,hello "source")',
    '[设置](/settings "source")',
    '[官方说明](https://example.test/docs)',
    '[Source][ref]\n\n[ref]: /settings\n[ref]: https://example.test/research',
    'Ready ([report.md](/viewer/document-id)).',
    'Ready ([report.pdf](https://example.test/report.pdf)).',
    'Ready ([Download report](https://example.test/download?id=1)).',
    '就绪（[打开报告](/viewer/document-id)）。',
    'Sources:\n\n- Configure settings\n- Do not hide this instruction',
  ]) {
    assert.equal(all(project(text), "sourceCitation").length, 0, text);
  }
});
