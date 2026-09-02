#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const [apiSource, knowledgeSource, uploadStoreSource, authBoundarySource, en, zh, es] = await Promise.all([
  readFile(new URL("../src/lib/api.ts", import.meta.url), "utf8"),
  readFile(new URL("../src/pages/Knowledge.tsx", import.meta.url), "utf8"),
  readFile(new URL("../src/stores/knowledgeUploads.ts", import.meta.url), "utf8"),
  readFile(new URL("../src/components/AuthSessionBoundary.tsx", import.meta.url), "utf8"),
  readFile(new URL("../src/lib/i18n/en.ts", import.meta.url), "utf8"),
  readFile(new URL("../src/lib/i18n/zh.ts", import.meta.url), "utf8"),
  readFile(new URL("../src/lib/i18n/es.ts", import.meta.url), "utf8"),
]);

test("Knowledge uploads expose byte progress and stop a stalled request", () => {
  assert.match(apiSource, /new XMLHttpRequest\(\)/);
  assert.match(apiSource, /xhr\.upload\.addEventListener\("progress"/);
  assert.match(apiSource, /DEFAULT_DOCUMENT_UPLOAD_STALL_TIMEOUT_MS = 120_000/);
  assert.match(apiSource, /DEFAULT_DOCUMENT_UPLOAD_PROCESSING_TIMEOUT_MS = 600_000/);
  assert.match(apiSource, /DEFAULT_DOCUMENT_UPLOAD_RECEIPT_RECONCILE_TIMEOUT_MS = 15_000/);
  assert.match(apiSource, /stalledPhase = "uploading";\s*xhr\.abort\(\)/);
  assert.match(apiSource, /stalledPhase = "processing";\s*xhr\.abort\(\)/);
  assert.match(apiSource, /new DocumentUploadStalledError\(stalledPhase\)/);
  assert.match(apiSource, /requestOptions\.signal\?\.addEventListener\("abort", abortRequest/);
  assert.match(apiSource, /requestOptions\.idempotencyKey \|\| createDocumentUploadIdempotencyKey\(\)/);
  assert.match(apiSource, /xhr\.setRequestHeader\("Idempotency-Key", idempotencyKey\)/);
  assert.match(apiSource, /documents\/upload-receipts\/\$\{encodeURIComponent\(idempotencyKey\)\}/);
  assert.match(apiSource, /stalledPhase === "processing"[\s\S]*?finishProcessingReconciliation\(\)/);
  assert.match(apiSource, /if \(uploadBytesSent\) \{\s*finishProcessingReconciliation\(\)/);
  assert.match(apiSource, /xhr\.status >= 500[\s\S]*?finishHttpFailureReconciliation/);
  assert.match(apiSource, /Invalid upload response[\s\S]*?finishHttpFailureReconciliation/);
  assert.match(apiSource, /latestToken && latestToken !== token/);
  assert.match(apiSource, /requestToken === getAuthToken\(\)/);
  assert.match(apiSource, /phase: "processing"[\s\S]*?percent: 100/);
});

test("Knowledge keeps independent upload cards with honest cancel, retry, and server-sized preflight", () => {
  assert.match(knowledgeSource, /typeof data\?\.max_upload_mb === "number"/);
  assert.match(knowledgeSource, /maxUploadMb \* 1024 \* 1024/);
  assert.doesNotMatch(knowledgeSource, /MAX_DOCUMENT_UPLOAD_BYTES/);
  assert.match(knowledgeSource, /useKnowledgeUploadStore\(\(state\) => state\.items\)/);
  assert.match(uploadStoreSource, /create<KnowledgeUploadState>/);
  assert.match(uploadStoreSource, /knowledgeUploadControllers = new Map<string, AbortController>/);
  assert.match(knowledgeSource, /item\.scopeId === uploadScopeId/);
  assert.match(knowledgeSource, /scopeId: uploadScopeId/);
  assert.match(knowledgeSource, /dismissUpload\(upload\.id\)/);
  assert.doesNotMatch(knowledgeSource, /controller\.abort\(\)[\s\S]*?uploadControllersRef\.current\.clear\(\)/);
  assert.match(knowledgeSource, /formatFileSize\(upload\.fileSize\)/);
  assert.match(knowledgeSource, /upload\.status === "uploading" \? `\$\{upload\.progress\}%`/);
  assert.match(knowledgeSource, /upload\.status === "processing"[\s\S]*?page\.knowledge\.processing_upload/);
  assert.match(knowledgeSource, /upload\.status === "uploading"[\s\S]*?cancelUpload\(upload\.id\)/);
  assert.match(knowledgeSource, /retryUpload\(upload\.id\)/);
  assert.match(knowledgeSource, /knowledgeUploadControllers\.has\(id\)/);
  assert.match(knowledgeSource, /idempotencyKey: item\.id/);
  assert.match(knowledgeSource, /recoveryUploadIdRef\.current = item\.id/);
  assert.match(knowledgeSource, /file\.name !== item\.fileName \|\| file\.size !== item\.fileSize/);
  assert.match(knowledgeSource, /void runUploadBatch\(\[retryItem\]\)/);
  assert.match(
    knowledgeSource,
    /role="progressbar"[\s\S]*?aria-valuenow=\{upload\.status === "uploading" \? upload\.progress : undefined\}/,
  );
  assert.match(knowledgeSource, /entry\.id !== item\.id/);
  assert.doesNotMatch(knowledgeSource, /setUploadingFiles\(files\.map\(\(f\) => f\.name\)\)/);

  const cancelActions = [...knowledgeSource.matchAll(/cancelUpload\(upload\.id\)/g)];
  assert.equal(cancelActions.length, 2, "grid and table should each expose one transfer cancel action");
  for (const action of cancelActions) {
    const guard = knowledgeSource.slice(Math.max(0, action.index - 600), action.index);
    assert.match(guard, /upload\.status === "uploading"/, "cancel must only be rendered during byte transfer");
  }
});

test("upload retries and receipt side effects stay within one authenticated principal", () => {
  assert.match(apiSource, /const requestPrincipalKey = expectedPrincipalKey \?\? authPrincipalKey\(token\)/);
  assert.match(apiSource, /authPrincipalKey\(latestToken\) === requestPrincipalKey/);
  assert.match(apiSource, /new DocumentUploadAuthChangedError\(\)/);
  assert.match(apiSource, /reconcileDocumentUploadReceipt\([\s\S]*?receiptReconcileTimeoutMs/);
  assert.match(apiSource, /originalError\.code === "document_upload_commit_uncertain"/);
  assert.match(knowledgeSource, /authPrincipalKey\(getAuthToken\(\)\) !== batchPrincipalKey/);
  assert.match(
    knowledgeSource,
    /authPrincipalKey\(getAuthToken\(\)\) !== batchPrincipalKey[\s\S]*?return;[\s\S]*?invalidateDocumentBrowseAndFolderTree\(\)/,
  );
  assert.match(
    knowledgeSource,
    /await invalidateDocumentBrowseAndFolderTree\(\);\s*if \(authPrincipalKey\(getAuthToken\(\)\) !== batchPrincipalKey\) return;/,
  );
  assert.match(knowledgeSource, /item\.principalKey === authPrincipalKey\(getAuthToken\(\)\)/);
  assert.match(authBoundarySource, /resetKnowledgeUploadsForAuthChange\(\)/);
});

test("processing receipt recovery survives reload without persisting or recreating File bytes", () => {
  assert.match(uploadStoreSource, /manor_knowledge_upload_recovery_v1/);
  assert.match(uploadStoreSource, /item\.status === "processing" \|\| item\.file === null/);
  assert.match(uploadStoreSource, /!item\.principalKey\.startsWith\("opaque:"\)/);
  assert.match(uploadStoreSource, /file: null/);
  assert.doesNotMatch(uploadStoreSource, /JSON\.stringify\([\s\S]{0,500}item\.file[,}]/);
  assert.match(knowledgeSource, /item\.file === null/);
  assert.match(knowledgeSource, /api\.documents\.reconcileUploadReceipt\(item\.id/);
});

test("Knowledge reveals the upload target and keeps cards scoped to that folder", () => {
  assert.match(knowledgeSource, /const targetIsVisible = \(/);
  assert.match(
    knowledgeSource,
    /if \(!targetIsVisible\) \{[\s\S]*?setSelectedWorkspaceId\(null\)[\s\S]*?setLibrarySection\("all"\)[\s\S]*?setSearch\(""\)/,
  );
  assert.match(
    knowledgeSource,
    /updateKnowledgeUrl\(\{[\s\S]*?folderId: targetFolderId[\s\S]*?workspaceId: null[\s\S]*?section: "all"[\s\S]*?search: null/,
  );
  assert.match(knowledgeSource, /const visibleUploadingFiles = useMemo/);
  assert.match(knowledgeSource, /normalizeKnowledgeFolderId\(item\.folderId\) === visibleFolderId/);
  assert.match(knowledgeSource, /selectedWorkspaceId \|\| librarySection !== "all" \|\| isSearching/);
  assert.match(
    knowledgeSource,
    /foldersAtLevel\.length === 0[\s\S]*?allDocuments\.length === 0[\s\S]*?visibleUploadingFiles\.length === 0/,
  );
  assert.match(knowledgeSource, /visibleUploadingFiles\.map\(\(upload\) =>/);
  assert.doesNotMatch(knowledgeSource, /uploadingFiles\.map\(\(upload\) =>/);
});

test("upload lifecycle labels are available in every shipped locale", () => {
  for (const source of [en, zh, es]) {
    for (const key of [
      "page.knowledge.processing_upload",
      "page.knowledge.upload_stalled",
      "page.knowledge.upload_stalled_short",
      "page.knowledge.retry_upload",
      "page.knowledge.select_original_file",
      "page.knowledge.select_original_file_mismatch",
      "page.knowledge.file_too_large_max_mb",
    ]) {
      assert.match(source, new RegExp(`"${key.replaceAll(".", "\\.")}"`));
    }
  }
});
