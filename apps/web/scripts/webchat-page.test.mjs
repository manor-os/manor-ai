import assert from "node:assert/strict";
import { test } from "node:test";
import { build } from "esbuild";

const bundle = await build({
  stdin: { contents: 'export * from "../src/lib/webchatPage.ts";', loader: "ts", resolveDir: new URL(".", import.meta.url).pathname },
  bundle: true, format: "esm", platform: "node", write: false, logLevel: "silent",
});
const { createWebchatModule, validWebchatPage, safeWebchatUrl, moveWebchatModule, webchatModuleVisible, webchatPageFingerprint, webchatPageForSave, webchatWorkspaceDisplayName, withWorkspaceResourcePreviews } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`);
const module = (id, side) => ({ id, side, type: "text", title: id, body: "" });

test("moving between sides preserves content and the requested ordering without mutating the draft", () => {
  const page = { version: 1, modules: [module("a", "left"), module("b", "left"), module("c", "right")] };
  const moved = moveWebchatModule(page, "b", "right", "c");
  assert.deepEqual(moved.modules.filter(item => item.side === "right").map(item => item.id), ["b", "c"]);
  assert.equal(page.modules[1].side, "left");
  const appended = moveWebchatModule(moved, "b", "left", null);
  assert.deepEqual(appended.modules.filter(item => item.side === "left").map(item => item.id), ["a", "b"]);
  assert.equal(moveWebchatModule(page, "missing", "left", null), page);
  assert.equal(moveWebchatModule(page, "a", "right", "b"), page);
});

test("untrusted page data cannot introduce executable modules, duplicate IDs or missing text fields", () => {
  for (const value of [null, { version: 2, modules: [] }, { version: 1, modules: [module("a", "left"), module("a", "right")] }, { version: 1, modules: [{ id: "a", side: "left", type: "html", html: "<script>" }] }, { version: 1, modules: [{ id: "a", side: "left", type: "text", body: "missing title" }] }]) {
    assert.equal(validWebchatPage(value), false);
  }
  assert.equal(validWebchatPage({ version: 1, modules: [module("a", "left")] }), true);
});

test("public links reject executable, credential-bearing and ambiguous URLs", () => {
  for (const url of ["javascript:alert(1)", "data:text/html,test", "//example.com", "https://user:pass@example.com", "https://example.com\\@evil.test", "https://example.com\n", "https://example.com:bad"]) assert.equal(safeWebchatUrl(url), "");
  assert.equal(safeWebchatUrl("https://example.com/help"), "https://example.com/help");
});

test("empty optional modules do not occupy public sidebars", () => {
  assert.equal(webchatModuleVisible({ id: "b", side: "left", type: "brand", name: "", logo_url: "", website: "" }), false);
  assert.equal(webchatModuleVisible({ ...module("a", "left"), title: "", body: "   " }), false);
  assert.equal(webchatModuleVisible({ id: "i", side: "right", type: "image", title: "Image", url: "", caption: "" }), false);
  assert.equal(webchatModuleVisible(module("a", "left")), true);
});

test("Workspace connectors require resolved public content and safe unique action fields", () => {
  const content = { id: "content", side: "left", type: "workspace_content", title: "About", source: "document", resource_id: "guide", resolved: { name: "Guide", body: "Public text", image_url: "", items: [] } };
  const action = { id: "action", side: "right", type: "workspace_action", title: "Book", description: "Request a tour", binding_id: "flow", fields: ["Name", "Date"], submit_label: "Send" };
  assert.equal(validWebchatPage({ version: 1, modules: [content, action] }), true);
  assert.equal(webchatModuleVisible(content), true);
  assert.equal(webchatModuleVisible({ ...content, resolved: null }), false);
  assert.equal(validWebchatPage({ version: 1, modules: [{ ...action, fields: ["Name", "Name"] }] }), false);
  assert.equal(validWebchatPage({ version: 1, modules: [{ ...action, fields: ["Name", "name"] }] }), false);
  assert.equal(validWebchatPage({ version: 1, modules: [{ ...action, fields: ["API token"] }] }), false);
  for (const reserved of ["fields", "FIELDS", "trigger", "webchat_session_id", "webchat_module_id", "webchat_submission_id", "webchat_channel_config_id"]) {
    assert.equal(validWebchatPage({ version: 1, modules: [{ ...action, fields: [reserved] }] }), false);
  }
  assert.equal(webchatModuleVisible({ ...action, title: "", description: "", fields: [], submit_label: "" }), true);
});

test("Webchat branding prefers the configured public Workspace identity", () => {
  assert.equal(webchatWorkspaceDisplayName({ name: "Internal name", identity_label: "Public company" }), "Public company");
  assert.equal(webchatWorkspaceDisplayName({ name: "Internal name", identity_label: "  " }), "Internal name");
  assert.deepEqual(
    (({ id: _id, ...module }) => module)(createWebchatModule("brand", "left", { name: "Public company", logo_url: "https://example.com/logo.png", website: "" })),
    { side: "left", type: "brand", name: "Public company", logo_url: "https://example.com/logo.png", website: "" },
  );
});

test("Workspace content previews resolve asynchronously without entering saved configuration", () => {
  const page = { version: 1, modules: [{ id: "guide", side: "left", type: "workspace_content", title: "Guide", source: "document", resource_id: "doc" }] };
  const resources = {
    brand: { name: "Acme", logo_url: "", website: "" },
    profile: { id: "workspace", name: "Acme", body: "", image_url: "", items: [] },
    documents: [{ id: "doc", name: "Visitor guide", body: "Welcome" }],
    documents_next_cursor: null,
    actions: [],
  };
  const preview = withWorkspaceResourcePreviews(page, resources);
  assert.equal(preview.modules[0].resolved.body, "Welcome");
  assert.deepEqual(webchatPageForSave(preview), page);
  assert.equal(withWorkspaceResourcePreviews(page, { ...resources, documents: [] }).modules[0].resolved, null);
});

test("page fingerprints ignore object key insertion order", () => {
  const first = { version: 1, modules: [{ id: "a", side: "left", type: "text", title: "Title", body: "Body" }] };
  const reordered = { modules: [{ body: "Body", type: "text", title: "Title", side: "left", id: "a" }], version: 1 };
  assert.equal(webchatPageFingerprint(first), webchatPageFingerprint(reordered));
});
