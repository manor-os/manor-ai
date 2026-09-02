import { expect, test, type Page, type Route } from "@playwright/test";

const fixtureUrl = "/e2e/fixtures/knowledge-upload-lifecycle.html";

async function openFixture(page: Page) {
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  await page.goto(fixtureUrl);
  await page.getByLabel("Choose upload file").setInputFiles({
    name: "interview-demo.mov",
    mimeType: "video/quicktime",
    buffer: Buffer.alloc(64 * 1024, 7),
  });
}

async function installAuthRetryXhr(page: Page, mode: "rotated" | "switched" | "impersonation") {
  await page.addInitScript(({ retryMode }) => {
    const token = (sub: string, entityId: string, issuedAt: number) => (
      `fixture.${btoa(JSON.stringify({ sub, entity_id: entityId, iat: issuedAt }))}.signature`
    );
    const oldToken = token("user-a", "entity-a", 1);
    const rotatedToken = token("user-a", "entity-a", 2);
    const switchedToken = token("user-b", "entity-b", 2);
    const ownerToken = token("support-owner", "entity-a", 1);
    const supportToken = token("impersonated-user", "entity-a", 1);
    const initialToken = retryMode === "impersonation" ? ownerToken : oldToken;
    const nextToken = retryMode === "rotated" ? rotatedToken : switchedToken;
    localStorage.setItem("manor_token", initialToken);
    if (retryMode === "impersonation") {
      sessionStorage.setItem("manor_impersonation_token", supportToken);
    }
    (window as any).__uploadInitialToken = retryMode === "impersonation" ? supportToken : initialToken;
    (window as any).__uploadNextToken = retryMode === "impersonation" ? ownerToken : nextToken;
    (window as any).__uploadHeaders = [];
    (window as any).__uploadAttempt = 0;
    class AuthRetryXHR extends EventTarget {
      upload = new EventTarget();
      responseText = "";
      status = 0;
      statusText = "";
      attempt = (window as any).__uploadAttempt++;

      open() {}

      setRequestHeader(name: string, value: string) {
        (window as any).__uploadHeaders.push([this.attempt, name, value]);
      }

      send() {
        this.upload.dispatchEvent(new ProgressEvent("loadstart"));
        this.upload.dispatchEvent(new ProgressEvent("progress", {
          lengthComputable: true,
          loaded: 1,
          total: 1,
        }));
        this.upload.dispatchEvent(new ProgressEvent("load"));
        queueMicrotask(() => {
          if (this.attempt === 0) {
            if (retryMode !== "impersonation") localStorage.setItem("manor_token", nextToken);
            this.status = 401;
            this.responseText = JSON.stringify({
              detail: retryMode === "impersonation"
                ? "Support session expired"
                : "Session expired",
            });
          } else {
            this.status = 201;
            this.responseText = JSON.stringify({ id: "fixture-document", name: "interview-demo.mov" });
          }
          this.dispatchEvent(new Event("load"));
        });
      }

      abort() {
        this.dispatchEvent(new Event("abort"));
      }
    }
    (window as any).XMLHttpRequest = AuthRetryXHR;
  }, { retryMode: mode });
}

async function installProcessingXhr(page: Page) {
  await page.addInitScript(() => {
    (window as any).__uploadHeaders = [];
    class ProcessingXHR extends EventTarget {
      upload = new EventTarget();
      responseText = "";
      status = 0;
      statusText = "";

      open() {}

      setRequestHeader(name: string, value: string) {
        (window as any).__uploadHeaders.push([name, value]);
      }

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
}

async function installLostResponseXhr(page: Page) {
  await page.addInitScript(() => {
    class LostResponseXHR extends EventTarget {
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
        queueMicrotask(() => this.dispatchEvent(new Event("error")));
      }

      abort() {
        this.dispatchEvent(new Event("abort"));
      }
    }
    (window as any).XMLHttpRequest = LostResponseXHR;
  });
}

async function installCommitted503Xhr(page: Page) {
  await page.addInitScript(() => {
    class Committed503XHR extends EventTarget {
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
        queueMicrotask(() => {
          this.status = 503;
          this.responseText = JSON.stringify({
            detail: {
              code: "document_upload_commit_uncertain",
              message: "Gateway lost the committed response",
            },
          });
          this.dispatchEvent(new Event("load"));
        });
      }

      abort() {
        this.dispatchEvent(new Event("abort"));
      }
    }
    (window as any).XMLHttpRequest = Committed503XHR;
  });
}

async function installInvalidCommittedResponseXhr(page: Page) {
  await page.addInitScript(() => {
    class InvalidCommittedResponseXHR extends EventTarget {
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
        queueMicrotask(() => {
          this.status = 201;
          this.responseText = "upstream injected a non-JSON success page";
          this.dispatchEvent(new Event("load"));
        });
      }

      abort() {
        this.dispatchEvent(new Event("abort"));
      }
    }
    (window as any).XMLHttpRequest = InvalidCommittedResponseXHR;
  });
}

test("upload identifies the file and completes after the server accepts it", async ({ page }) => {
  let pending: Route | null = null;
  let idempotencyKey = "";
  await page.route("**/api/v1/documents/upload", (route) => {
    idempotencyKey = route.request().headers()["idempotency-key"] || "";
    pending = route;
  });
  await openFixture(page);
  await expect(page.getByText("interview-demo.mov · 64.0 KB", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Start upload" }).click();
  await expect.poll(() => pending !== null).toBe(true);
  // Playwright pauses the intercepted upload body together with the response,
  // so Chromium cannot emit its native `upload.load`/processing transition
  // until the route settles. The transport source-contract test covers that
  // native event; this browser test verifies the user-visible request lifecycle.
  await expect(page.getByRole("status")).toHaveText("uploading");
  expect(idempotencyKey).toBe("fixture-upload-request-0001");
  await pending!.fulfill({ status: 201, json: { id: "fixture-document", name: "interview-demo.mov" } });
  await expect(page.getByRole("status")).toHaveText("success");
});

test("stalled transfer settles as a retryable failure", async ({ page }) => {
  await page.route("**/api/v1/documents/upload", () => {
    // Deliberately leave the intercepted request pending. The fixture uses a
    // two-second stall budget so this proves the UI cannot spin forever.
  });
  await openFixture(page);
  await page.getByRole("button", { name: "Start upload" }).click();
  await expect(page.getByRole("status")).toHaveText("failed", { timeout: 5_000 });
  await expect(page.getByRole("alert")).toHaveText("Upload stalled");
  await expect(page.getByRole("button", { name: "Retry" })).toBeEnabled();
});

test("failed upload remains retryable and cancellation settles", async ({ page }) => {
  const pending: Route[] = [];
  const idempotencyKeys: string[] = [];
  await page.route("**/api/v1/documents/upload", (route) => {
    idempotencyKeys.push(route.request().headers()["idempotency-key"] || "");
    pending.push(route);
  });
  await openFixture(page);
  await page.getByRole("button", { name: "Start upload" }).click();
  await expect.poll(() => pending.length).toBe(1);
  await pending.shift()!.fulfill({ status: 503, json: { detail: "Upload service unavailable" } });
  await expect(page.getByRole("status")).toHaveText("failed");
  await expect(page.getByRole("alert")).toContainText("Backend unavailable");

  await page.getByRole("button", { name: "Retry" }).click();
  await expect.poll(() => pending.length).toBe(1);
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("status")).toHaveText("cancelled");
  expect(idempotencyKeys).toEqual([
    "fixture-upload-request-0001",
    "fixture-upload-request-0001",
  ]);
});

test("server processing is bounded and remains retryable", async ({ page }) => {
  await installProcessingXhr(page);
  await page.route("**/api/v1/documents/upload-receipts/**", async (route) => {
    await route.fulfill({ status: 404, json: { detail: "Upload receipt not found" } });
  });
  await openFixture(page);

  await page.getByRole("button", { name: "Start upload" }).click();
  await expect(page.getByRole("status")).toHaveText("processing");
  await expect(page.getByRole("button", { name: "Cancel" })).toBeDisabled();
  await expect(page.getByRole("status")).toHaveText("failed", { timeout: 5_000 });
  await expect(page.getByRole("alert")).toHaveText("Upload stalled");
  await expect(page.getByRole("button", { name: "Retry" })).toBeEnabled();
  const headers = await page.evaluate(() => (window as any).__uploadHeaders);
  expect(headers).toContainEqual(["Idempotency-Key", "fixture-upload-request-0001"]);
});

test("processing timeout resolves a receipt committed after browser abort", async ({ page }) => {
  await installProcessingXhr(page);
  let receiptPath = "";
  await page.route("**/api/v1/documents/upload-receipts/**", async (route) => {
    receiptPath = new URL(route.request().url()).pathname;
    await route.fulfill({
      status: 200,
      json: { id: "fixture-document", name: "interview-demo.mov" },
    });
  });
  await openFixture(page);

  await page.getByRole("button", { name: "Start upload" }).click();

  await expect(page.getByRole("status")).toHaveText("processing");
  await expect(page.getByRole("status")).toHaveText("success", { timeout: 5_000 });
  expect(receiptPath).toContain("/documents/upload-receipts/fixture-upload-request-0001");
});

test("a lost response resolves the committed upload receipt", async ({ page }) => {
  await installLostResponseXhr(page);
  let receiptRequests = 0;
  await page.route("**/api/v1/documents/upload-receipts/**", async (route) => {
    receiptRequests += 1;
    await route.fulfill({
      status: 200,
      json: { id: "fixture-document", name: "interview-demo.mov" },
    });
  });
  await openFixture(page);

  await page.getByRole("button", { name: "Start upload" }).click();

  await expect(page.getByRole("status")).toHaveText("success", { timeout: 5_000 });
  expect(receiptRequests).toBe(1);
});

test("a 5xx after transfer resolves the committed upload receipt", async ({ page }) => {
  await installCommitted503Xhr(page);
  let receiptRequests = 0;
  await page.route("**/api/v1/documents/upload-receipts/**", async (route) => {
    receiptRequests += 1;
    if (receiptRequests === 1) {
      await route.fulfill({ status: 404, json: { detail: "Upload receipt not visible yet" } });
      return;
    }
    await route.fulfill({
      status: 200,
      json: { id: "fixture-document", name: "interview-demo.mov" },
    });
  });
  await openFixture(page);

  await page.getByRole("button", { name: "Start upload" }).click();

  await expect(page.getByRole("status")).toHaveText("success", { timeout: 5_000 });
  expect(receiptRequests).toBe(2);
});

test("an invalid success response resolves the committed upload receipt", async ({ page }) => {
  await installInvalidCommittedResponseXhr(page);
  let receiptRequests = 0;
  await page.route("**/api/v1/documents/upload-receipts/**", async (route) => {
    receiptRequests += 1;
    await route.fulfill({
      status: 200,
      json: { id: "fixture-document", name: "interview-demo.mov" },
    });
  });
  await openFixture(page);

  await page.getByRole("button", { name: "Start upload" }).click();

  await expect(page.getByRole("status")).toHaveText("success", { timeout: 5_000 });
  expect(receiptRequests).toBe(1);
});

test("a late 401 cannot clear a token that rotated during upload", async ({ page }) => {
  await installAuthRetryXhr(page, "rotated");
  await openFixture(page);

  await page.getByRole("button", { name: "Start upload" }).click();

  await expect(page.getByRole("status")).toHaveText("success");
  const tokens = await page.evaluate(() => ({
    initial: (window as any).__uploadInitialToken,
    next: (window as any).__uploadNextToken,
    current: localStorage.getItem("manor_token"),
  }));
  expect(tokens.current).toBe(tokens.next);
  const authorizationHeaders = await page.evaluate(() => (
    (window as any).__uploadHeaders.filter((entry: string[]) => entry[1] === "Authorization")
  ));
  expect(authorizationHeaders).toEqual([
    [0, "Authorization", `Bearer ${tokens.initial}`],
    [1, "Authorization", `Bearer ${tokens.next}`],
  ]);
});

test("a late 401 never resends upload bytes after an account switch", async ({ page }) => {
  await installAuthRetryXhr(page, "switched");
  await openFixture(page);

  await page.getByRole("button", { name: "Start upload" }).click();

  await expect(page.getByRole("status")).toHaveText("failed");
  await expect(page.getByRole("alert")).toContainText("authenticated account changed");
  const authorizationHeaders = await page.evaluate(() => (
    (window as any).__uploadHeaders.filter((entry: string[]) => entry[1] === "Authorization")
  ));
  expect(authorizationHeaders).toHaveLength(1);
});

test("an expired impersonation upload stops before the owner session can receive it", async ({ page }) => {
  await installAuthRetryXhr(page, "impersonation");
  await openFixture(page);

  await page.getByRole("button", { name: "Start upload" }).click();

  await expect(page.getByRole("status")).toHaveText("failed");
  await expect(page.getByRole("alert")).toContainText("authenticated account changed");
  expect(await page.evaluate(() => sessionStorage.getItem("manor_impersonation_token"))).toBeNull();
  const authorizationHeaders = await page.evaluate(() => (
    (window as any).__uploadHeaders.filter((entry: string[]) => entry[1] === "Authorization")
  ));
  expect(authorizationHeaders).toHaveLength(1);
});

test("upload controls remain usable at a mobile viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openFixture(page);
  await expect(page.getByText("interview-demo.mov · 64.0 KB", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Start upload" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Cancel" })).toBeVisible();
  await expect.poll(() => page.evaluate(() => (
    document.documentElement.scrollWidth <= window.innerWidth
  ))).toBe(true);
});
