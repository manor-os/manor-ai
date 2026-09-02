import assert from "node:assert/strict";
import { test } from "node:test";
import { build } from "esbuild";

const bundle = await build({
  entryPoints: [new URL("../src/lib/fileReferences.ts", import.meta.url).pathname],
  bundle: true, format: "esm", platform: "node", write: false,
});
const refs = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`);
const name = "AI_SDE_20小时课程_Agenda与课程内容.md";

test("filenames and encoded paths cannot become canonical Document IDs", () => {
  for (const id of [name, encodeURIComponent(name), encodeURIComponent(encodeURIComponent(name)), "Workspaces/Demo/report.md", "../report.md", "report.pdf", "/viewer/doc_1"]) {
    assert.equal(refs.viewerPathForDocumentId(id), null);
    assert.equal(refs.generatedFileDocumentId({ document_id: id }), "");
    assert.equal(refs.generatedFileOpenReference({ document_id: id, fs_path: "Workspaces/Demo/report.md" }), "/viewer/Workspaces%2FDemo%2Freport.md");
  }
  assert.equal(refs.viewerPathForDocumentId("01M1B8KT380NRNZ9DGX8ZVGF7A"), "/viewer/01M1B8KT380NRNZ9DGX8ZVGF7A");
  assert.equal(refs.generatedFileOpenReference({ document_id: "doc_exact", viewer_url: `/viewer/${encodeURIComponent(name)}` }), "/viewer/doc_exact");
});

test("legacy viewer names retain bounded decoding candidates without accepting traversal", () => {
  for (const target of [name, encodeURIComponent(name), encodeURIComponent(encodeURIComponent(name))]) {
    assert.ok(refs.legacyViewerFileCandidates(`/viewer/${target}`).includes(name));
  }
  assert.deepEqual(refs.legacyViewerFileCandidates("/viewer/doc_exact"), []);
  assert.deepEqual(refs.legacyViewerFileCandidates("/viewer/..%252Freport.md"), []);
  assert.deepEqual(refs.legacyViewerFileCandidates("https://other.test/viewer/report.md"), []);
  assert.deepEqual(refs.legacyViewerFileCandidates("/viewer/%E0%A4%A.md"), []);
});
