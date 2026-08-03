#!/usr/bin/env node
// ShareDialog "General access" truthfulness (docs/PERMISSIONS_DESIGN_ZH.md §13.1,
// docs/PERMISSIONS_UX_DESIGN_ZH.md §1.2/§2.4).
//
// The old dialog claimed "Only people in the list above can access" whenever
// no link share existed — false for visibility=entity items, where every
// entity member can read the item AND find it via search/RAG
// (packages/core/services/document_access.py::user_can_read_document falls
// back: entity/public/NULL visibility → all entity staff read).
//
// These tests pin the pure copy-selection helper: the General access section
// must always describe BOTH dimensions — internal visibility and link
// sharing — and must fall back to "entity" exactly like the backend does.
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { build } from "esbuild";

const entryPoint = `
  export { effectiveVisibility, generalAccessCopy } from "../src/components/permissions/generalAccessCopy.ts";
`;

const bundled = await build({
  stdin: {
    contents: entryPoint,
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

const { effectiveVisibility, generalAccessCopy } = await import(moduleUrl);

// ── effectiveVisibility mirrors the backend fallback ─────────────────────
// document_access.py: `visibility or Visibility.ENTITY`.

test("effectiveVisibility falls back to entity exactly like the backend", () => {
  assert.equal(effectiveVisibility(undefined), "entity");
  assert.equal(effectiveVisibility(null), "entity");
  assert.equal(effectiveVisibility(""), "entity");
  assert.equal(effectiveVisibility("bogus"), "entity");
  assert.equal(effectiveVisibility("entity"), "entity");
  assert.equal(effectiveVisibility("private"), "private");
  assert.equal(effectiveVisibility("workspace"), "workspace");
  assert.equal(effectiveVisibility("public"), "public");
});

// ── Internal-access line ──────────────────────────────────────────────────

test("private visibility → 'only you and the people listed' (neutral)", () => {
  const { internal } = generalAccessCopy({ mode: "restricted", visibility: "private" });
  assert.equal(internal.key, "permissions.share.desc.internal_private");
  assert.equal(internal.tone, "neutral");
});

test("workspace visibility → workspace-members line (neutral)", () => {
  const { internal } = generalAccessCopy({ mode: "restricted", visibility: "workspace" });
  assert.equal(internal.key, "permissions.share.desc.internal_workspace");
  assert.equal(internal.tone, "neutral");
});

test("entity visibility → org-wide warning line, NOT the false list-only claim", () => {
  const { internal } = generalAccessCopy({ mode: "restricted", visibility: "entity" });
  assert.equal(internal.key, "permissions.share.desc.internal_entity");
  assert.equal(internal.tone, "warning");
});

test("missing/NULL visibility is treated as entity-wide (the backend truth)", () => {
  for (const visibility of [undefined, null]) {
    const { internal } = generalAccessCopy({ mode: "restricted", visibility });
    assert.equal(internal.key, "permissions.share.desc.internal_entity");
    assert.equal(internal.tone, "warning");
  }
});

test("public visibility also renders the org-wide warning line", () => {
  const { internal } = generalAccessCopy({ mode: "restricted", visibility: "public" });
  assert.equal(internal.key, "permissions.share.desc.internal_entity");
  assert.equal(internal.tone, "warning");
});

test("internal line is independent of the link mode (orthogonal dimensions)", () => {
  for (const mode of ["restricted", "anyone_link", "domain"]) {
    const { internal } = generalAccessCopy({ mode, visibility: "entity", entityDomain: "acme.com" });
    assert.equal(internal.key, "permissions.share.desc.internal_entity");
  }
});

// ── Link line ─────────────────────────────────────────────────────────────

test("no link share → 'link sharing off', never a list-only access claim", () => {
  const { link } = generalAccessCopy({ mode: "restricted", visibility: "entity" });
  assert.equal(link.key, "permissions.share.desc.link_off");
  assert.equal(link.tone, "neutral");
});

test("anyone_link keeps existing copy (+ approval variant)", () => {
  assert.equal(
    generalAccessCopy({ mode: "anyone_link", visibility: "private" }).link.key,
    "permissions.share.desc.anyone_link",
  );
  assert.equal(
    generalAccessCopy({ mode: "anyone_link", visibility: "private", needsApproval: true }).link.key,
    "permissions.share.desc.anyone_link_approval",
  );
});

test("domain mode keeps existing copy with the domain param", () => {
  const { link } = generalAccessCopy({
    mode: "domain",
    visibility: "private",
    entityDomain: "acme.com",
  });
  assert.equal(link.key, "permissions.share.desc.domain");
  assert.deepEqual(link.params, { domain: "acme.com" });
  const approval = generalAccessCopy({
    mode: "domain",
    visibility: "private",
    entityDomain: "acme.com",
    needsApproval: true,
  }).link;
  assert.equal(approval.key, "permissions.share.desc.domain_approval");
  assert.deepEqual(approval.params, { domain: "acme.com" });
});

test("classification=restricted overrides the link line (error tone)", () => {
  for (const mode of ["restricted", "anyone_link", "domain"]) {
    const { link } = generalAccessCopy({
      mode,
      visibility: "entity",
      classification: "restricted",
      entityDomain: "acme.com",
    });
    assert.equal(link.key, "permissions.share.desc.restricted_doc");
    assert.equal(link.tone, "error");
  }
});

// ── Source-level guards ───────────────────────────────────────────────────

const shareDialogSource = await readFile(
  new URL("../src/components/permissions/ShareDialog.tsx", import.meta.url),
  "utf8",
);

test("ShareDialog renders copy through the helper and dropped the false claim", () => {
  assert.match(shareDialogSource, /from "\.\/generalAccessCopy"/);
  assert.match(shareDialogSource, /generalAccessCopy\(/);
  assert.doesNotMatch(shareDialogSource, /permissions\.share\.desc\.list_only/);
});

test("ShareDialog exposes an internal-visibility switcher hook", () => {
  assert.match(shareDialogSource, /onChangeVisibility\?:/);
  assert.match(shareDialogSource, /permissions\.share\.visibility_label/);
  // Fallback pointer for call sites without a switcher (per UX doc §2.2 the
  // control otherwise lives in file properties).
  assert.match(shareDialogSource, /permissions\.share\.visibility_readonly_hint/);
});

test("both knowledge containers and FileViewer wire the visibility switcher", async () => {
  const knowledge = await readFile(
    new URL("../src/pages/Knowledge.tsx", import.meta.url),
    "utf8",
  );
  const fileViewer = await readFile(
    new URL("../src/pages/FileViewer.tsx", import.meta.url),
    "utf8",
  );
  const knowledgeWirings = knowledge.match(/onChangeVisibility=\{/g) || [];
  assert.ok(
    knowledgeWirings.length >= 2,
    "Knowledge.tsx should wire onChangeVisibility for both the doc and folder share containers",
  );
  assert.match(fileViewer, /onChangeVisibility=\{/);
  // Doc dialogs go through permissions_v1, folder dialog through
  // folderPermissions.setProperties.
  assert.match(knowledge, /api\.permissionsV1\.setVisibility/);
  assert.match(knowledge, /api\.folderPermissions\.setProperties\(folder\.id/);
  assert.match(fileViewer, /api\.permissionsV1\.setVisibility/);
});

test("all three locales carry the new keys and dropped the misleading one", async () => {
  const requiredKeys = [
    "permissions.share.desc.internal_private",
    "permissions.share.desc.internal_workspace",
    "permissions.share.desc.internal_entity",
    "permissions.share.desc.link_off",
    "permissions.share.visibility_label",
    "permissions.share.visibility_readonly_hint",
  ];
  for (const locale of ["en", "zh", "es"]) {
    const source = await readFile(
      new URL(`../src/lib/i18n/${locale}.ts`, import.meta.url),
      "utf8",
    );
    for (const key of requiredKeys) {
      assert.ok(source.includes(`"${key}"`), `${locale}.ts missing ${key}`);
    }
    assert.ok(
      !source.includes('"permissions.share.desc.list_only"'),
      `${locale}.ts still contains the misleading list_only key`,
    );
  }
});
