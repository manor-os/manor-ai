#!/usr/bin/env node
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import ts from "typescript";

async function loadKnowledgeLayout() {
  const sourceUrl = new URL("../src/lib/knowledgeLayout.ts", import.meta.url);
  const source = await readFile(sourceUrl, "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ES2022,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;

  const tempDir = await mkdtemp(join(tmpdir(), "knowledge-layout-"));
  const tempFile = join(tempDir, "knowledgeLayout.mjs");
  await writeFile(tempFile, compiled, "utf8");
  const mod = await import(`file://${tempFile}`);
  await rm(tempDir, { recursive: true, force: true });
  return mod;
}

const {
  getKnowledgeBrowseParams,
  getKnowledgeDocumentsForView,
} = await loadKnowledgeLayout();
const knowledgeSource = await readFile(new URL("../src/pages/Knowledge.tsx", import.meta.url), "utf8");
const apiSource = await readFile(new URL("../src/lib/api.ts", import.meta.url), "utf8");

function doc(id, name, folderId, overrides = {}) {
  return {
    id,
    name,
    folder_id: folderId,
    file_type: name.split(".").pop(),
    file_size: overrides.file_size ?? 100,
    created_at: overrides.created_at ?? `2026-06-${String(overrides.day ?? 1).padStart(2, "0")}T00:00:00Z`,
    ...overrides,
  };
}

test("directory browsing keeps every direct file instead of applying a page window", () => {
  const documents = Array.from({ length: 30 }, (_, index) =>
    doc(`direct-${String(index).padStart(2, "0")}`, `file-${String(index).padStart(2, "0")}.md`, "folder-a", { day: index + 1 }),
  );
  documents.push(doc("nested", "nested.md", "folder-b", { day: 31 }));
  documents.push(doc("root", "root.md", null, { day: 32 }));

  const visible = getKnowledgeDocumentsForView({
    documents,
    currentFolderId: "folder-a",
    favoriteDocIds: new Set(),
    fileTypeFilter: "all",
    isSearching: false,
    librarySection: "all",
    selectedWorkspaceId: null,
    sortKey: "name",
  });

  assert.equal(visible.length, 30);
  assert.deepEqual(visible.map((item) => item.id), documents.slice(0, 30).map((item) => item.id));
});

test("root browsing shows only root files while search remains global", () => {
  const documents = [
    doc("root", "root.md", null, { day: 2 }),
    doc("child", "child.md", "folder-a", { day: 3 }),
    doc("other", "other.md", "folder-b", { day: 1 }),
  ];

  const rootVisible = getKnowledgeDocumentsForView({
    documents,
    currentFolderId: null,
    favoriteDocIds: new Set(),
    fileTypeFilter: "all",
    isSearching: false,
    librarySection: "all",
    selectedWorkspaceId: null,
    sortKey: "date",
  });
  assert.deepEqual(rootVisible.map((item) => item.id), ["root"]);

  const searchVisible = getKnowledgeDocumentsForView({
    documents,
    currentFolderId: "folder-a",
    favoriteDocIds: new Set(),
    fileTypeFilter: "all",
    isSearching: true,
    librarySection: "all",
    selectedWorkspaceId: null,
    sortKey: "date",
  });
  assert.deepEqual(searchVisible.map((item) => item.id), ["child", "root", "other"]);
});

test("Knowledge browse requests scope directory views while search remains global", () => {
  assert.deepEqual(
    getKnowledgeBrowseParams({ currentFolderId: null, searchTerm: "", selectedWorkspaceId: null }),
    { folder_id: "root" },
  );
  assert.deepEqual(
    getKnowledgeBrowseParams({ currentFolderId: "folder-a", searchTerm: "", selectedWorkspaceId: null }),
    { folder_id: "folder-a" },
  );
  assert.deepEqual(
    getKnowledgeBrowseParams({ currentFolderId: "folder-a", searchTerm: "daily", selectedWorkspaceId: null }),
    { search: "daily" },
  );
  assert.deepEqual(
    getKnowledgeBrowseParams({ currentFolderId: "folder-a", librarySection: "recent", searchTerm: "", selectedWorkspaceId: null }),
    { scope: "all" },
  );
  assert.deepEqual(
    getKnowledgeBrowseParams({ currentFolderId: "folder-a", librarySection: "favorites", searchTerm: "", selectedWorkspaceId: null }),
    { scope: "all" },
  );
});

test("Knowledge page uses the browse endpoint without showing pagination controls", () => {
  assert.match(apiSource, /function\s+browseDocuments\s*\(params\?:/);
  assert.match(apiSource, /browse:\s*browseDocuments/);
  assert.match(apiSource, /\/documents\/browse\?\$\{q\}/);
  assert.match(apiSource, /tree:\s*\(\)\s*=>\s*request<DocumentFolderInfo\[\]>\("\/documents\/folder-tree"\)/);
  assert.match(apiSource, /async\s+function\s+listAllDocuments\s*\(params\?:/);
  assert.match(apiSource, /listAll:\s*listAllDocuments/);
  assert.match(knowledgeSource, /queryFn:\s*\(\)\s*=>\s*api\.documents\.browse\(/);
  assert.match(knowledgeSource, /queryFn:\s*\(\)\s*=>\s*api\.folders\.tree\(\)/);
  assert.match(knowledgeSource, /return\s+data\?\.folders\s+\|\|\s+\[\]/);
  assert.doesNotMatch(knowledgeSource, /data\?\.folder_tree\s+\|\|\s+\[\]/);
  assert.doesNotMatch(knowledgeSource, /api\.folders\.list\(\)/);
  assert.doesNotMatch(knowledgeSource, /<Pagination\b/);
});

test("Knowledge folder state comes from the route before the breadcrumb is hydrated", () => {
  assert.match(knowledgeSource, /const\s+routeFolderId\s*=\s*selectedWorkspaceId\s*\?\s*null\s*:\s*normalizeKnowledgeFolderId\(searchParams\.get\("folder_id"\)\);/);
  assert.match(knowledgeSource, /const\s+currentFolderId\s*=\s*selectedWorkspaceId\s*\?\s*null\s*:\s*\(routeFolderId\s*\|\|\s*folderPath\[folderPath\.length\s*-\s*1\]\?\.id\s*\|\|\s*null\);/);
  assert.match(knowledgeSource, /queryKey:\s*\["documents-browse",\s*searchTerm,\s*currentFolderId,\s*selectedWorkspaceId,\s*librarySection\]/);
});

test("batch selection actions stay viewport-floating and suppress per-item trash toasts", () => {
  assert.match(knowledgeSource, /import\s+\{\s*createPortal\s*\}\s+from\s+"react-dom";/);
  assert.match(knowledgeSource, /\.kb-batch-bar\s*\{[^}]*position:\s*fixed;/s);
  assert.match(knowledgeSource, /\.kb-batch-bar\s*\{[^}]*bottom:\s*calc\(max\(18px,\s*env\(safe-area-inset-bottom\)\s*\+\s*18px\)\);/s);
  assert.match(knowledgeSource, /\.kb-batch-bar\s*\{[^}]*max-width:\s*min\(calc\(100vw\s*-\s*32px\),\s*760px\);/s);
  assert.match(knowledgeSource, /createPortal\(\s*batchActionBar,\s*document\.body\s*\)/);
  assert.match(knowledgeSource, /mutationFn:\s*\(\{\s*id\s*\}:\s*\{\s*id:\s*string;\s*silent\?:\s*boolean\s*\}\)\s*=>\s*api\.documents\.trash\(id\)/);
  assert.match(knowledgeSource, /onSuccess:\s*\(_result,\s*\{\s*silent\s*=\s*false\s*\}\)\s*=>/);
  assert.match(knowledgeSource, /if\s*\(!silent\)\s+toast\.success\(t\("page\.knowledge\.moved_to_trash"\)\);/);
  assert.match(knowledgeSource, /docIds\.map\(\(id\)\s*=>\s*trashMutation\.mutateAsync\(\{\s*id,\s*silent:\s*true\s*\}\)\)/);
});
