import { expect, test, type Page, type Route } from "@playwright/test";

const fixtureUrl = "/e2e/fixtures/knowledge-upload-page.html";
const folderId = "01FIXTUREKNOWLEDGEFOLDER1";

function browsePayload(folders: any[] = [], maxUploadMb = 500) {
  return {
    folders,
    documents: [],
    items: [],
    total: 0,
    total_documents: 0,
    total_files: 0,
    total_folders: folders.length,
    total_size: 0,
    storage_used_mb: 0,
    storage_limit_mb: 500,
    max_upload_mb: maxUploadMb,
  };
}

function fixtureFolder() {
  return {
    id: folderId,
    entity_id: "01FIXTUREKNOWLEDGEENTITY1",
    name: "Other folder",
    parent_id: null,
    owner_id: "01FIXTUREKNOWLEDGEUSER001",
    visibility: "private",
    classification: "internal",
    client_visible: false,
    document_count: 0,
    current_user_capabilities: ["view", "edit", "upload_to", "manage_metadata"],
  };
}

async function mockKnowledgeApi(
  page: Page,
  { includeFolder = false, maxUploadMb = 500 } = {},
) {
  const uploads: Route[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/documents/upload")) {
      uploads.push(route);
      return;
    }
    if (url.pathname.endsWith("/documents/browse")) {
      const requestedFolder = url.searchParams.get("folder_id");
      const inFolder = Boolean(requestedFolder && requestedFolder !== "root");
      await route.fulfill({
        json: browsePayload(!inFolder && includeFolder ? [fixtureFolder()] : [], maxUploadMb),
      });
      return;
    }
    if (url.pathname.endsWith("/documents/folder-tree")) {
      await route.fulfill({ json: includeFolder ? [fixtureFolder()] : [] });
      return;
    }
    if (url.pathname.endsWith("/fs/wiki-index")) {
      await route.fulfill({ json: { pages: [], missing_links: [] } });
      return;
    }
    await route.fulfill({ json: [] });
  });
  return uploads;
}

async function startUpload(page: Page, fileName: string) {
  await page.getByLabel("Choose upload file").setInputFiles({
    name: fileName,
    mimeType: "text/plain",
    buffer: Buffer.from("fixture upload"),
  });
  await page.getByRole("button", { name: "Use defaults" }).click();
}

test("empty Knowledge folder renders its pending upload card with cancellation", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  const uploads = await mockKnowledgeApi(page);
  await page.goto(fixtureUrl);
  await expect(page.getByText("Empty folder", { exact: true }).first()).toBeVisible();

  await startUpload(page, "empty-folder-upload.txt");

  await expect.poll(() => uploads.length).toBe(1);
  await expect(page.getByText("empty-folder-upload.txt", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Cancel" })).toBeEnabled();
  await expect(page.getByText("Empty folder", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Cancel" }).click();
});

test("pending upload card stays in its target folder when navigation changes", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  const uploads = await mockKnowledgeApi(page, { includeFolder: true });
  await page.goto(fixtureUrl);

  await startUpload(page, "root-only-upload.txt");
  await expect.poll(() => uploads.length).toBe(1);
  await expect(page.getByText("root-only-upload.txt", { exact: true })).toBeVisible();

  await page.getByText("Other folder", { exact: true }).click();

  await expect(page.getByText("root-only-upload.txt", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Empty folder", { exact: true }).first()).toBeVisible();
  await uploads[0].abort("aborted");
});

test("upload started from search reveals its real target and stays actionable", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  const uploads = await mockKnowledgeApi(page);
  await page.goto(fixtureUrl);

  const search = page.getByPlaceholder("Search documents...");
  await search.fill("missing document");
  await expect(search).toHaveValue("missing document");

  await startUpload(page, "search-origin-upload.txt");

  await expect.poll(() => uploads.length).toBe(1);
  await expect(search).toHaveValue("");
  await expect(page.getByText("search-origin-upload.txt", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Cancel" })).toBeEnabled();
  await uploads[0].abort("aborted");
});

test("Knowledge uses the server-provided upload size limit", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  const uploads = await mockKnowledgeApi(page, { maxUploadMb: 1 });
  await page.goto(fixtureUrl);

  await page.getByLabel("Choose upload file").setInputFiles({
    name: "too-large.txt",
    mimeType: "text/plain",
    buffer: Buffer.alloc(2 * 1024 * 1024, 1),
  });
  await page.getByRole("button", { name: "Use defaults" }).click();

  await expect(page.getByText("File too large. Maximum upload size is 1MB.")).toBeVisible();
  expect(uploads).toHaveLength(0);
});

test("server processing does not offer a cancellation the backend cannot honor", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  await mockKnowledgeApi(page);
  await page.goto(fixtureUrl);
  await page.evaluate(() => {
    class ProcessingXHR extends EventTarget {
      upload = new EventTarget();
      responseText = "";
      status = 0;
      statusText = "";

      open() {}

      setRequestHeader() {}

      send() {
        this.upload.dispatchEvent(new ProgressEvent("loadstart"));
        this.upload.dispatchEvent(new ProgressEvent("progress", {
          lengthComputable: true,
          loaded: 1,
          total: 1,
        }));
        this.upload.dispatchEvent(new ProgressEvent("load"));
      }

      abort() {
        this.dispatchEvent(new Event("abort"));
      }
    }
    (window as any).XMLHttpRequest = ProcessingXHR;
  });

  await startUpload(page, "processing-upload.txt");

  await expect(page.getByText("Processing", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Cancel" })).toHaveCount(0);
  await expect(page.getByText("processing-upload.txt", { exact: true })).toBeVisible();
});

test("processing upload survives Knowledge route unmount and remount", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  await mockKnowledgeApi(page);
  await page.goto(fixtureUrl);
  await page.evaluate(() => {
    class PersistentProcessingXHR extends EventTarget {
      upload = new EventTarget();
      responseText = "";
      status = 0;
      statusText = "";

      open() {}

      setRequestHeader() {}

      send() {
        this.upload.dispatchEvent(new ProgressEvent("loadstart"));
        this.upload.dispatchEvent(new ProgressEvent("progress", {
          lengthComputable: true,
          loaded: 1,
          total: 1,
        }));
        this.upload.dispatchEvent(new ProgressEvent("load"));
        (window as any).__completeKnowledgeUpload = () => {
          this.status = 201;
          this.responseText = JSON.stringify({
            id: "persistent-document",
            name: "persistent-processing.txt",
          });
          this.dispatchEvent(new Event("load"));
        };
      }

      abort() {
        (window as any).__knowledgeUploadAborted = true;
        this.dispatchEvent(new Event("abort"));
      }
    }
    (window as any).XMLHttpRequest = PersistentProcessingXHR;
  });

  await startUpload(page, "persistent-processing.txt");
  await expect(page.getByText("Processing", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Hide Knowledge" }).click();
  await expect(page.getByText("Knowledge route unmounted", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => Boolean((window as any).__knowledgeUploadAborted))).toBe(false);

  await page.getByRole("button", { name: "Show Knowledge" }).click();
  await expect(page.getByText("persistent-processing.txt", { exact: true })).toBeVisible();
  await expect(page.getByText("Processing", { exact: true })).toBeVisible();
  await page.evaluate(() => (window as any).__completeKnowledgeUpload());
  await expect(page.getByText("persistent-processing.txt", { exact: true })).toHaveCount(0);
});

test("completion from a previous account cannot toast or refresh the current account", async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("manor_locale", "en");
    const token = (sub: string, entityId: string) => (
      `fixture.${btoa(JSON.stringify({ sub, entity_id: entityId }))}.signature`
    );
    localStorage.setItem("manor_token", token("user-a", "entity-a"));
    (window as any).__otherKnowledgeToken = token("user-b", "entity-b");
  });
  await mockKnowledgeApi(page);
  await page.goto(fixtureUrl);
  await page.evaluate(() => {
    class AccountSwitchXHR extends EventTarget {
      upload = new EventTarget();
      responseText = "";
      status = 0;
      statusText = "";

      open() {}

      setRequestHeader() {}

      send() {
        this.upload.dispatchEvent(new ProgressEvent("loadstart"));
        this.upload.dispatchEvent(new ProgressEvent("progress", {
          lengthComputable: true,
          loaded: 1,
          total: 1,
        }));
        this.upload.dispatchEvent(new ProgressEvent("load"));
        (window as any).__completePreviousAccountUpload = () => {
          localStorage.setItem("manor_token", (window as any).__otherKnowledgeToken);
          this.status = 201;
          this.responseText = JSON.stringify({
            id: "previous-account-document",
            name: "private-previous-account.txt",
            pii_detected: true,
            classification: "confidential",
          });
          this.dispatchEvent(new Event("load"));
        };
      }

      abort() {
        this.dispatchEvent(new Event("abort"));
      }
    }
    (window as any).XMLHttpRequest = AccountSwitchXHR;
  });

  await startUpload(page, "private-previous-account.txt");
  await expect(page.getByText("Processing", { exact: true })).toBeVisible();
  await page.evaluate(() => (window as any).__completePreviousAccountUpload());

  await expect(page.getByText("private-previous-account.txt", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Document uploaded", { exact: true })).toHaveCount(0);
  await expect(page.getByText(/sensitive data/i)).toHaveCount(0);
});

test("processing upload restores receipt reconciliation after a full page reload", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  await mockKnowledgeApi(page);
  let receiptRequests = 0;
  await page.route("**/api/v1/documents/upload-receipts/**", async (route) => {
    receiptRequests += 1;
    if (receiptRequests === 1) {
      await route.fulfill({ status: 404, json: { detail: "Upload receipt not visible yet" } });
      return;
    }
    await route.fulfill({
      status: 200,
      json: {
        id: "reloaded-document",
        name: "reload-processing.txt",
        classification: "internal",
        pii_detected: false,
      },
    });
  });
  await page.goto(fixtureUrl);
  await page.evaluate(() => {
    class ReloadProcessingXHR extends EventTarget {
      upload = new EventTarget();
      responseText = "";
      status = 0;
      statusText = "";

      open() {}

      setRequestHeader() {}

      send() {
        this.upload.dispatchEvent(new ProgressEvent("loadstart"));
        this.upload.dispatchEvent(new ProgressEvent("progress", {
          lengthComputable: true,
          loaded: 1,
          total: 1,
        }));
        this.upload.dispatchEvent(new ProgressEvent("load"));
      }

      abort() {
        this.dispatchEvent(new Event("abort"));
      }
    }
    (window as any).XMLHttpRequest = ReloadProcessingXHR;
  });

  await startUpload(page, "reload-processing.txt");
  await expect(page.getByText("Processing", { exact: true })).toBeVisible();
  await expect.poll(() => page.evaluate(() => (
    sessionStorage.getItem("manor_knowledge_upload_recovery_v1") || ""
  ))).toContain("reload-processing.txt");

  await page.reload();

  await expect(page.getByText("reload-processing.txt", { exact: true })).toBeVisible();
  await expect(page.getByText("Processing", { exact: true })).toBeVisible();
  await expect(page.getByText("reload-processing.txt", { exact: true })).toHaveCount(0, { timeout: 5_000 });
  expect(receiptRequests).toBe(2);
  expect(await page.evaluate(() => (
    sessionStorage.getItem("manor_knowledge_upload_recovery_v1")
  ))).toBeNull();
});

test("failed reload recovery reselects the original file and reuses its upload key", async ({ page }) => {
  const originalName = "reload-rolled-back.txt";
  const originalBytes = Buffer.from("fixture upload");
  const uploadId = "reload-rollback-upload-0001";
  await page.addInitScript(({ fileName, fileSize, idempotencyKey }) => {
    const token = `fixture.${btoa(JSON.stringify({
      sub: "01FIXTUREKNOWLEDGEUSER001",
      entity_id: "01FIXTUREKNOWLEDGEENTITY1",
    }))}.signature`;
    localStorage.setItem("manor_locale", "en");
    localStorage.setItem("manor_token", token);
    sessionStorage.setItem("manor_knowledge_upload_recovery_v1", JSON.stringify({
      version: 1,
      items: [{
        id: idempotencyKey,
        scopeId: "01FIXTUREKNOWLEDGEENTITY1:01FIXTUREKNOWLEDGEUSER001",
        principalKey: JSON.stringify([
          "01FIXTUREKNOWLEDGEUSER001",
          "01FIXTUREKNOWLEDGEENTITY1",
        ]),
        fileName,
        fileSize,
        folderId: null,
        createdAt: Date.now(),
      }],
    }));
  }, { fileName: originalName, fileSize: originalBytes.length, idempotencyKey: uploadId });
  const uploads = await mockKnowledgeApi(page);
  await page.route("**/api/v1/documents/upload-receipts/**", async (route) => {
    await route.fulfill({ status: 410, json: { detail: "Original commit rolled back" } });
  });
  await page.goto(fixtureUrl);

  await expect(page.getByText(originalName, { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Select original file" })).toBeEnabled();
  await page.getByRole("button", { name: "Select original file" }).click();
  await page.getByLabel("Select original file").setInputFiles({
    name: "wrong-file.txt",
    mimeType: "text/plain",
    buffer: originalBytes,
  });
  await expect(page.getByText(`Select the original file: ${originalName}`)).toBeVisible();
  expect(uploads).toHaveLength(0);

  await page.getByRole("button", { name: "Select original file" }).click();
  await page.getByLabel("Select original file").setInputFiles({
    name: originalName,
    mimeType: "text/plain",
    buffer: originalBytes,
  });
  await expect.poll(() => uploads.length).toBe(1);
  expect(uploads[0].request().headers()["idempotency-key"]).toBe(uploadId);
  await uploads[0].fulfill({
    status: 201,
    json: { id: "recovered-document", name: originalName },
  });
  await expect(page.getByText(originalName, { exact: true })).toHaveCount(0);
});
