import { expect, request as pwRequest, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import JSZip from "jszip";
import PptxGenJS from "pptxgenjs";
import * as XLSX from "xlsx";

const API = process.env.E2E_API ?? "http://localhost:8000";
const RUN_DOCKER_E2E = process.env.E2E_DOCKER_DOC_EDITOR === "1";

test.skip(!RUN_DOCKER_E2E, "Set E2E_DOCKER_DOC_EDITOR=1 for the live document editor test");

test("floating AI Edit rolls back an unaccepted preview and restores its opener", async ({ page }) => {
  test.setTimeout(60_000);
  const api = await pwRequest.newContext({ baseURL: API });
  const consoleErrors: string[] = [];
  let token = "";
  let documentId = "";

  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => consoleErrors.push(error.message));

  try {
    const login = await api.post("/api/v1/auth/login", {
      data: { email: "demo@manor.local", password: "manor-demo" },
    });
    expect(login.ok(), `login failed: ${login.status()}`).toBeTruthy();
    token = (await login.json()).access_token;
    const headers = { Authorization: `Bearer ${token}` };
    const upload = await api.post("/api/v1/documents/upload", {
      headers,
      multipart: {
        file: {
          name: `floating-ai-edit-${Date.now()}.txt`,
          mimeType: "text/plain",
          buffer: Buffer.from("AI Edit focus regression", "utf8"),
        },
      },
    });
    expect(upload.ok(), `document upload failed: ${upload.status()}`).toBeTruthy();
    documentId = (await upload.json()).id;

    await page.setViewportSize({ width: 800, height: 900 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/login");
    await page.evaluate((accessToken) => {
      window.localStorage.setItem("manor_token", accessToken);
    }, token);
    await page.goto(`/editor/${documentId}`);
    await page.evaluate(() => {
      window.addEventListener("manor:open-editor-live-chat", ((event: CustomEvent) => {
        (window as Window & { __e2eAiEditAdapter?: unknown }).__e2eAiEditAdapter =
          event.detail.adapter;
      }) as EventListener, { once: true });
    });
    const aiEditButton = page.getByRole("button", { name: "AI edit", exact: true });
    await aiEditButton.click();

    const panel = page.locator("#floating-chat-panel");
    const editor = page.locator("textarea.text-editor-page");
    const originalContent = await editor.inputValue();
    const previewContent = `${originalContent}\nUnaccepted AI Edit preview`;
    await expect(panel).toHaveAttribute("data-open", "true");
    await expect.poll(() => panel.evaluate((element) => (
      Math.round(element.getBoundingClientRect().width)
    ))).toBe(380);
    await expect.poll(() => page.evaluate(() => (
      document.activeElement?.classList.contains("chat-composer-rich-editor") || false
    ))).toBe(true);

    await page.evaluate(async (nextContent) => {
      type E2EAdapter = {
        read: () => string;
        beginTurn: (meta: Record<string, unknown>) => boolean | void | Promise<boolean | void>;
        preview: (
          content: string,
          meta: Record<string, unknown>,
        ) => boolean | void | Promise<boolean | void>;
        complete: (
          content: string,
          meta: Record<string, unknown>,
        ) => boolean | void | Promise<boolean | void>;
      };
      const adapter = (window as Window & { __e2eAiEditAdapter?: E2EAdapter })
        .__e2eAiEditAdapter;
      if (!adapter) throw new Error("AI Edit adapter was not captured");
      const turnId = "e2e-unaccepted-preview";
      adapter.read();
      await adapter.beginTurn({
        complete: false,
        phase: "preview",
        source: "assistant-stream",
        turnId,
      });
      await adapter.preview(nextContent, {
        complete: false,
        phase: "preview",
        source: "assistant-stream",
        turnId,
      });
      await adapter.complete(nextContent, {
        complete: true,
        phase: "complete",
        source: "assistant-stream",
        turnId,
      });
    }, previewContent);
    await expect(editor).toHaveValue(previewContent);
    await expect(editor).toHaveAttribute("readonly", "");
    await panel.locator(".chat-composer-rich-editor").focus();
    await page.keyboard.press("Escape");
    await expect(panel).toHaveAttribute("data-open", "false");
    await expect(panel).toHaveAttribute("inert", "");
    await expect(editor).toHaveValue(originalContent);
    await expect(editor).not.toHaveAttribute("readonly", "");
    await expect(aiEditButton).toBeFocused();
    expect(consoleErrors).toEqual([]);
  } finally {
    await api.dispose();
    if (token && documentId) {
      const cleanup = await pwRequest.newContext({ baseURL: API });
      const trashed = await cleanup.post(`/api/v1/documents/${documentId}/trash`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      expect(trashed.ok(), `document cleanup failed: ${trashed.status()}`).toBeTruthy();
      await cleanup.dispose();
    }
  }
});

test("PPTX chart graphic frames stay visible and duplicate as native OOXML objects", async ({ page }) => {
  test.setTimeout(120_000);
  const consoleErrors: string[] = [];
  let token = "";
  let documentId = "";

  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => consoleErrors.push(error.message));

  try {
    await page.goto("/login");
    const login = await page.request.post("/api/v1/auth/login", {
      data: { email: "demo@manor.local", password: "manor-demo" },
    });
    expect(login.ok(), `login failed: ${login.status()}`).toBeTruthy();
    token = (await login.json()).access_token;
    const headers = { Authorization: `Bearer ${token}` };
    const chartDeck = await readFile(new URL(
      "../public/assets/samples/artifacts/slides/blue-commons-impact-report.pptx",
      import.meta.url,
    ));
    const upload = await page.request.post("/api/v1/documents/upload", {
      headers,
      multipart: {
        file: {
          name: `pptx-chart-editor-${Date.now()}.pptx`,
          mimeType: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
          buffer: chartDeck,
        },
      },
    });
    expect(upload.ok(), `chart deck upload failed: ${upload.status()}`).toBeTruthy();
    documentId = (await upload.json()).id;

    await page.evaluate((accessToken) => {
      window.localStorage.setItem("manor_token", accessToken);
    }, token);
    await page.goto(`/editor/${documentId}`);
    const editor = page.locator(".presentation-editor");
    const loadFailure = page.getByText("Failed to load document for editing", { exact: false });
    await expect(editor.or(loadFailure)).toBeVisible();
    expect(await loadFailure.isVisible(), consoleErrors.join("\n")).toBeFalsy();
    await expect(editor).toBeVisible();
    await page.getByRole("tab", { name: "Slide 6", exact: true }).click();

    const frame = page.locator(".presentation-editor-slide-frame");
    const graphicPreview = frame.locator('[role="img"][aria-label="Object"]');
    await expect(graphicPreview).toHaveCount(1);
    await expect(graphicPreview.locator("img")).toBeVisible({ timeout: 30_000 });
    await expect.poll(() => graphicPreview.locator("img").evaluate((image) => (
      (image as HTMLImageElement).naturalWidth
    )), { timeout: 30_000 }).toBeGreaterThan(0);

    const shapeHosts = frame.locator(":scope > [data-presentation-shape-type]");
    const imageHosts = frame.locator(':scope > [data-presentation-shape-type="image"]');
    const originalShapeCount = await shapeHosts.count();
    const originalImageCount = await imageHosts.count();
    await frame.locator(':scope > [data-presentation-shape-type="graphic"]').click({ position: { x: 12, y: 12 } });
    await page.keyboard.press("Control+d");
    await expect(shapeHosts).toHaveCount(originalShapeCount + 1);
    await expect(frame.locator(':scope > [data-presentation-shape-type="graphic"]')).toHaveCount(2);
    await expect(frame.locator(':scope > [data-presentation-shape-type="graphic"] [role="img"] img')).toHaveCount(2);
    await expect(imageHosts).toHaveCount(originalImageCount);
    expect(consoleErrors.filter((message) => /module script|maximum update depth|too many re-renders/i.test(message))).toEqual([]);
  } finally {
    if (token && documentId) {
      await page.request.post(`/api/v1/documents/${documentId}/trash`, {
        headers: { Authorization: `Bearer ${token}` },
      });
    }
  }
});

test("plain-text and AI history restore selections while the media dialog remains stable", async ({ page }) => {
  const api = await pwRequest.newContext({ baseURL: API });
  const suffix = Date.now();
  const originalText = "Doc editor history\nsecond line";
  const originalMarkdown = "# Async load\n\nOriginal markdown.";
  const uploadedDocumentIds: string[] = [];
  const consoleErrors: string[] = [];
  let token = "";

  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => consoleErrors.push(error.message));

  try {
    const login = await api.post("/api/v1/auth/login", {
      data: { email: "demo@manor.local", password: "manor-demo" },
    });
    expect(login.ok(), `login failed: ${login.status()}`).toBeTruthy();
    token = (await login.json()).access_token;
    const headers = { Authorization: `Bearer ${token}` };

    const upload = async (name: string, mimeType: string, content: string | Buffer) => {
      const response = await api.post("/api/v1/documents/upload", {
        headers,
        multipart: {
          file: { name, mimeType, buffer: Buffer.isBuffer(content) ? content : Buffer.from(content, "utf8") },
        },
      });
      expect(response.ok(), `${name} upload failed: ${response.status()}`).toBeTruthy();
      const document = await response.json();
      uploadedDocumentIds.push(document.id);
      return document;
    };

    const textDocument = await upload(`doc-history-${suffix}.txt`, "text/plain", originalText);
    const markdownDocument = await upload(`doc-load-${suffix}.md`, "text/markdown", originalMarkdown);
    const queuedSourceText = "Source document before queued save.";
    const queuedTargetText = "Target document must stay unchanged.";
    const queuedSourceDocument = await upload(`doc-save-source-${suffix}.txt`, "text/plain", queuedSourceText);
    const queuedTargetDocument = await upload(`doc-save-target-${suffix}.txt`, "text/plain", queuedTargetText);
    const failedSaveText = "This content should remain unchanged after failed saves.";
    const failedSaveDocument = await upload(`doc-save-failure-${suffix}.txt`, "text/plain", failedSaveText);
    const saveStatusDocument = await upload(`doc-save-status-${suffix}.txt`, "text/plain", "Initial save status content.");
    const retryOrderDocument = await upload(`doc-save-retry-order-${suffix}.txt`, "text/plain", "Initial retry order content.");
    const workbook = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(workbook, XLSX.utils.aoa_to_sheet([["Original workbook cell"]]), "Sheet1");
    const spreadsheetDocument = await upload(
      `doc-save-sheet-${suffix}.xlsx`,
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      XLSX.write(workbook, { type: "buffer", bookType: "xlsx" }) as Buffer,
    );
    const presentation = new PptxGenJS();
    presentation.layout = "LAYOUT_WIDE";
    const presentationSlide = presentation.addSlide();
    presentationSlide.addText("Original presentation text", { x: 1, y: 1, w: 8, h: 1 });
    const presentationOutput = await presentation.write({ outputType: "arraybuffer", compression: true });
    const presentationDocument = await upload(
      `doc-save-presentation-${suffix}.pptx`,
      "application/vnd.openxmlformats-officedocument.presentationml.presentation",
      Buffer.from(presentationOutput as ArrayBuffer),
    );
    const richTextDocument = await upload(
      `media-dialog-${suffix}.rtf`,
      "application/rtf",
      "<p>Media dialog regression probe</p>",
    );

    await page.addInitScript((accessToken) => {
      window.localStorage.setItem("manor_token", accessToken as string);
    }, token);
    await page.goto(`/editor/${textDocument.id}`);

    const rejectCookies = page.getByRole("button", { name: "Reject all", exact: true });
    if (await rejectCookies.isVisible()) await rejectCookies.click();
    const skipTour = page.getByRole("button", { name: "Skip", exact: true });
    if (await skipTour.isVisible()) await skipTour.click();

    const editor = page.getByPlaceholder("Start typing...", { exact: true });
    const undo = page.getByRole("button", { name: "Undo (Ctrl+Z)", exact: true });
    const redo = page.getByRole("button", { name: "Redo (Ctrl+Shift+Z)", exact: true });
    await expect(editor).toHaveValue(originalText);
    await expect(undo).toBeDisabled();
    await expect(redo).toBeDisabled();

    await editor.evaluate((element, initialText) => {
      const textarea = element as HTMLTextAreaElement;
      textarea.focus();
      textarea.setSelectionRange(textarea.value.length, textarea.value.length, "none");
      textarea.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true }));
      const valueSetter = Object.getOwnPropertyDescriptor(
        HTMLTextAreaElement.prototype,
        "value",
      )?.set;
      if (!valueSetter) throw new Error("textarea value setter is unavailable");
      valueSetter.call(textarea, `${textarea.value}zh`);
      textarea.dispatchEvent(new InputEvent("input", {
        bubbles: true,
        composed: true,
        data: "zh",
        inputType: "insertCompositionText",
        isComposing: true,
      }));
      textarea.dispatchEvent(new CompositionEvent("compositionend", {
        bubbles: true,
        data: "中",
      }));
      valueSetter.call(textarea, `${initialText}中`);
      textarea.dispatchEvent(new InputEvent("input", {
        bubbles: true,
        composed: true,
        data: "中",
        inputType: "insertFromComposition",
        isComposing: false,
      }));
      valueSetter.call(textarea, `${initialText}中!`);
      textarea.dispatchEvent(new InputEvent("input", {
        bubbles: true,
        composed: true,
        data: "!",
        inputType: "insertText",
        isComposing: false,
      }));
    }, originalText);
    await expect(editor).toHaveValue(`${originalText}中!`);
    await undo.click();
    await expect(editor).toHaveValue(`${originalText}中`);
    await undo.click();
    await expect(editor).toHaveValue(originalText);
    await redo.click();
    await expect(editor).toHaveValue(`${originalText}中`);
    await undo.click();
    await expect(editor).toHaveValue(originalText);

    await editor.fill(`${originalText}X`);
    await expect(undo).toBeEnabled();
    await undo.click();
    await expect(editor).toHaveValue(originalText);
    await expect(redo).toBeEnabled();
    await redo.click();
    await expect(editor).toHaveValue(`${originalText}X`);
    await expect.poll(() => editor.evaluate((element) => ({
      start: (element as HTMLTextAreaElement).selectionStart,
      end: (element as HTMLTextAreaElement).selectionEnd,
    }))).toEqual({ start: originalText.length + 1, end: originalText.length + 1 });

    await editor.pressSequentially("Y");
    await expect(editor).toHaveValue(`${originalText}XY`);

    await undo.click();
    await undo.click();
    await expect(editor).toHaveValue(originalText);

    const backwardStart = 4;
    const backwardEnd = 10;
    await editor.evaluate((element, selection) => {
      const textarea = element as HTMLTextAreaElement;
      textarea.focus();
      textarea.setSelectionRange(selection.start, selection.end, "backward");
    }, { start: backwardStart, end: backwardEnd });
    await editor.pressSequentially("Z");
    await expect(editor).toHaveValue(
      `${originalText.slice(0, backwardStart)}Z${originalText.slice(backwardEnd)}`,
    );
    await undo.click();
    await expect(editor).toHaveValue(originalText);
    await expect.poll(() => editor.evaluate((element) => {
      const textarea = element as HTMLTextAreaElement;
      return {
        start: textarea.selectionStart,
        end: textarea.selectionEnd,
        direction: textarea.selectionDirection,
      };
    })).toEqual({ start: backwardStart, end: backwardEnd, direction: "backward" });

    await page.evaluate(() => {
      type ApplyMeta = { complete: boolean; phase?: "preview" | "complete"; source: "assistant-stream"; turnId?: string };
      type LiveEditAdapter = {
        read: () => string;
        beginTurn: (meta: ApplyMeta) => boolean | void | Promise<boolean | void>;
        preview: (content: string, meta: ApplyMeta) => boolean | void | Promise<boolean | void>;
        complete: (content: string, meta: ApplyMeta) => boolean | void | Promise<boolean | void>;
      };
      const testWindow = window as typeof window & { __docEditorLiveAdapter?: LiveEditAdapter };
      window.addEventListener("manor:open-editor-live-chat", (event) => {
        testWindow.__docEditorLiveAdapter = (
          event as CustomEvent<{ adapter?: LiveEditAdapter }>
        ).detail.adapter;
      }, { once: true });
    });
    await page.getByRole("button", { name: "AI edit", exact: true }).click();
    await expect.poll(() => page.evaluate(() => (
      typeof (window as typeof window & { __docEditorLiveAdapter?: unknown }).__docEditorLiveAdapter
    ))).toBe("object");
    const aiRevision = "AI replaced the plain-text document.";
    await page.evaluate(async (nextContent) => {
      type ApplyMeta = { complete: boolean; phase?: "preview" | "complete"; source: "assistant-stream"; turnId?: string };
      type LiveEditAdapter = {
        read: () => string;
        beginTurn: (meta: ApplyMeta) => boolean | void | Promise<boolean | void>;
        preview: (content: string, meta: ApplyMeta) => boolean | void | Promise<boolean | void>;
        complete: (content: string, meta: ApplyMeta) => boolean | void | Promise<boolean | void>;
      };
      const adapter = (
        window as typeof window & { __docEditorLiveAdapter?: LiveEditAdapter }
      ).__docEditorLiveAdapter;
      if (!adapter) throw new Error("AI edit adapter was not captured");
      const turnId = "e2e-plain-text-ai-history";
      adapter.read();
      await adapter.beginTurn({ complete: false, phase: "preview", source: "assistant-stream", turnId });
      await adapter.preview(nextContent, { complete: false, phase: "preview", source: "assistant-stream", turnId });
      await adapter.complete(nextContent, { complete: true, phase: "complete", source: "assistant-stream", turnId });
    }, aiRevision);
    await expect(editor).toHaveValue(aiRevision);
    await page.locator("#floating-chat-panel")
      .getByRole("group", { name: "AI edit preview controls" })
      .getByRole("button", { name: "Accept", exact: true })
      .click();
    await expect(page.locator(".doc-editor-live-preview-bar")).toHaveCount(0);
    await expect(undo).toBeEnabled();
    await undo.click();
    await expect(editor).toHaveValue(originalText);

    let releaseMarkdownDownload = () => {};
    const markdownDownloadGate = new Promise<void>((resolve) => {
      releaseMarkdownDownload = resolve;
    });
    const markdownDownloadPattern = `**/documents/${markdownDocument.id}/preview/content**`;
    await page.route(markdownDownloadPattern, async (route) => {
      await markdownDownloadGate;
      await route.continue();
    });
    await page.goto(`/editor/${markdownDocument.id}`);
    const markdownEditor = page.getByPlaceholder("Write your markdown here...", { exact: true });
    await expect(markdownEditor).toBeHidden();
    const markdownDownloadResponse = page.waitForResponse((response) => (
      response.url().includes(`/documents/${markdownDocument.id}/preview/content`)
    ));
    releaseMarkdownDownload();
    await markdownDownloadResponse;
    await expect(markdownEditor).toHaveValue(originalMarkdown);
    const editedMarkdown = `${originalMarkdown}\n\nLocal edit survives.`;
    await markdownEditor.fill(editedMarkdown);
    await expect(markdownEditor).toHaveValue(editedMarkdown);
    await page.unroute(markdownDownloadPattern);
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(page.locator(".manor-editor-header").getByText("Saved", { exact: true })).toBeVisible();

    await page.goto(`/editor/${queuedSourceDocument.id}`);
    const queuedSourceEditor = page.getByPlaceholder("Start typing...", { exact: true });
    await expect(queuedSourceEditor).toHaveValue(queuedSourceText);
    const queuedSourceRevision = `${queuedSourceText} Local revision.`;
    await queuedSourceEditor.fill(queuedSourceRevision);
    await expect(page.locator(".manor-editor-header").getByText("Unsaved changes", { exact: true })).toBeVisible();
    await page.evaluate((targetDocumentId) => {
      window.history.pushState({}, "", `/editor/${targetDocumentId}`);
      window.dispatchEvent(new PopStateEvent("popstate"));
    }, queuedTargetDocument.id);
    const queuedTargetEditor = page.getByPlaceholder("Start typing...", { exact: true });
    await expect(queuedTargetEditor).toHaveValue(queuedTargetText);
    await expect.poll(async () => {
      const response = await api.get(`/api/v1/documents/${queuedSourceDocument.id}/content`, { headers });
      return response.ok() ? (await response.json()).content : null;
    }).toBe(queuedSourceRevision);
    const untouchedTarget = await api.get(`/api/v1/documents/${queuedTargetDocument.id}/content`, { headers });
    expect(untouchedTarget.ok()).toBeTruthy();
    expect((await untouchedTarget.json()).content).toBe(queuedTargetText);

    await page.goto(`/editor/${failedSaveDocument.id}`);
    const failedSaveEditor = page.getByPlaceholder("Start typing...", { exact: true });
    await expect(failedSaveEditor).toHaveValue(failedSaveText);
    let failedSaveAttempts = 0;
    const failedSavePattern = `**/documents/${failedSaveDocument.id}/file**`;
    await page.route(failedSavePattern, async (route) => {
      if (route.request().method() === "PUT") {
        failedSaveAttempts += 1;
        await route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "Regression save failure" }) });
        return;
      }
      await route.continue();
    });
    await failedSaveEditor.fill("Unsaved content that must report a failure.");
    await page.evaluate((targetDocumentId) => {
      window.history.pushState({}, "", `/editor/${targetDocumentId}`);
      window.dispatchEvent(new PopStateEvent("popstate"));
    }, queuedTargetDocument.id);
    await expect(queuedTargetEditor).toHaveValue(queuedTargetText);
    await expect(page.getByText(`Failed to save ${failedSaveDocument.name}: Regression save failure`, { exact: true })).toBeVisible();
    expect(failedSaveAttempts).toBe(3);
    await page.unroute(failedSavePattern);
    const failedSaveContent = await api.get(`/api/v1/documents/${failedSaveDocument.id}/content`, { headers });
    expect(failedSaveContent.ok()).toBeTruthy();
    expect((await failedSaveContent.json()).content).toBe(failedSaveText);

    await page.goto(`/editor/${spreadsheetDocument.id}`);
    const formulaInput = page.locator(".manor-editor-main input").first();
    await expect(formulaInput).toHaveValue("Original workbook cell");
    const updatedWorkbookCell = "Workbook edit survives navigation";
    await formulaInput.fill(updatedWorkbookCell);
    await expect(page.locator(".manor-editor-header").getByText("Unsaved changes", { exact: true })).toBeVisible();
    await page.evaluate((targetDocumentId) => {
      window.history.pushState({}, "", `/editor/${targetDocumentId}`);
      window.dispatchEvent(new PopStateEvent("popstate"));
    }, queuedTargetDocument.id);
    await expect(queuedTargetEditor).toHaveValue(queuedTargetText);
    await expect.poll(async () => {
      const response = await api.get(`/api/v1/documents/${spreadsheetDocument.id}/download`, { headers });
      if (!response.ok()) return null;
      const savedWorkbook = XLSX.read(await response.body(), { type: "buffer" });
      return savedWorkbook.Sheets[savedWorkbook.SheetNames[0]].A1?.v;
    }).toBe(updatedWorkbookCell);

    await page.goto(`/editor/${presentationDocument.id}`);
    await expect(page.locator(".presentation-editor")).toBeVisible();
    await page.evaluate(() => {
      type ApplyMeta = { complete: boolean; phase?: "preview" | "complete"; source: "assistant-stream"; turnId?: string };
      type LiveEditAdapter = {
        read: () => string;
        beginTurn: (meta: ApplyMeta) => boolean | void | Promise<boolean | void>;
        preview: (content: string, meta: ApplyMeta) => boolean | void | Promise<boolean | void>;
        complete: (content: string, meta: ApplyMeta) => boolean | void | Promise<boolean | void>;
      };
      type LiveEditDetail = { adapter?: LiveEditAdapter };
      const testWindow = window as typeof window & {
        __docEditorPresentationAdapter?: LiveEditAdapter;
      };
      window.addEventListener("manor:open-editor-live-chat", (event) => {
        const detail = (event as CustomEvent<LiveEditDetail>).detail;
        testWindow.__docEditorPresentationAdapter = detail.adapter;
      }, { once: true });
    });
    await page.getByRole("button", { name: "AI edit", exact: true }).click();
    await expect.poll(() => page.evaluate(() => (
      typeof (window as typeof window & { __docEditorPresentationAdapter?: unknown }).__docEditorPresentationAdapter
    ))).toBe("object");
    const updatedPresentationText = "Presentation edit survives navigation";
    await page.evaluate(async (replacementText) => {
      type ApplyMeta = { complete: boolean; phase?: "preview" | "complete"; source: "assistant-stream"; turnId?: string };
      type LiveEditAdapter = {
        read: () => string;
        beginTurn: (meta: ApplyMeta) => boolean | void | Promise<boolean | void>;
        preview: (content: string, meta: ApplyMeta) => boolean | void | Promise<boolean | void>;
        complete: (content: string, meta: ApplyMeta) => boolean | void | Promise<boolean | void>;
      };
      const testWindow = window as typeof window & {
        __docEditorPresentationAdapter?: LiveEditAdapter;
      };
      const adapter = testWindow.__docEditorPresentationAdapter;
      if (!adapter) throw new Error("Presentation live-edit adapter was not captured");
      const state = JSON.parse(adapter.read()) as {
        slides: Array<{ shapes: Array<{ paragraphs?: Array<{ text: string }> }> }>;
      };
      const paragraph = state.slides.flatMap((slide) => slide.shapes)
        .flatMap((shape) => shape.paragraphs || [])[0];
      if (!paragraph) throw new Error("Editable presentation paragraph was not found");
      const originalText = paragraph.text;
      paragraph.text = replacementText;
      paragraph.edits = [{ start: 0, end: originalText.length, text: replacementText }];
      const turnId = "e2e-presentation-ai-history";
      await adapter.beginTurn({ complete: false, phase: "preview", source: "assistant-stream", turnId });
      await adapter.preview(JSON.stringify(state), { complete: false, phase: "preview", source: "assistant-stream", turnId });
      await adapter.complete(JSON.stringify(state), { complete: true, phase: "complete", source: "assistant-stream", turnId });
    }, updatedPresentationText);
    await page.locator("#floating-chat-panel")
      .getByRole("group", { name: "AI edit preview controls" })
      .getByRole("button", { name: "Accept", exact: true })
      .click();
    await expect(page.locator(".doc-editor-live-preview-bar")).toHaveCount(0);
    await page.evaluate((targetDocumentId) => {
      window.history.pushState({}, "", `/editor/${targetDocumentId}`);
      window.dispatchEvent(new PopStateEvent("popstate"));
    }, queuedTargetDocument.id);
    await expect(queuedTargetEditor).toHaveValue(queuedTargetText);
    await expect.poll(async () => {
      const response = await api.get(`/api/v1/documents/${presentationDocument.id}/download`, { headers });
      if (!response.ok()) return false;
      const archive = await JSZip.loadAsync(await response.body());
      const slideParts = Object.keys(archive.files).filter((path) => /^ppt\/slides\/slide\d+\.xml$/.test(path));
      const slideXml = (await Promise.all(slideParts.map((path) => archive.file(path)?.async("text") || ""))).join("\n");
      return slideXml.includes(updatedPresentationText);
    }).toBe(true);

    await page.goto(`/editor/${saveStatusDocument.id}`);
    const saveStatusEditor = page.getByPlaceholder("Start typing...", { exact: true });
    await expect(saveStatusEditor).toHaveValue("Initial save status content.");
    let releaseFirstSave = () => {};
    let markFirstSaveStarted = () => {};
    const firstSaveGate = new Promise<void>((resolve) => { releaseFirstSave = resolve; });
    const firstSaveStarted = new Promise<void>((resolve) => { markFirstSaveStarted = resolve; });
    let firstSaveHeld = false;
    const saveStatusFilePattern = `**/documents/${saveStatusDocument.id}/file**`;
    await page.route(saveStatusFilePattern, async (route) => {
      if (route.request().method() === "PUT" && !firstSaveHeld) {
        firstSaveHeld = true;
        markFirstSaveStarted();
        await firstSaveGate;
      }
      await route.continue();
    });
    const firstRevision = "First revision is being saved.";
    const secondRevision = "Second revision must remain unsaved until persisted.";
    await saveStatusEditor.fill(firstRevision);
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await firstSaveStarted;
    await saveStatusEditor.fill(secondRevision);
    await expect(page.locator(".manor-editor-header").getByText("Unsaved changes", { exact: true })).toBeVisible();
    const firstSaveResponse = page.waitForResponse((response) => (
      response.url().includes(`/documents/${saveStatusDocument.id}/file`)
      && response.request().method() === "PUT"
    ));
    releaseFirstSave();
    await firstSaveResponse;
    await expect(page.locator(".manor-editor-header").getByText("Unsaved changes", { exact: true })).toBeVisible();
    const secondSaveResponse = page.waitForResponse((response) => (
      response.url().includes(`/documents/${saveStatusDocument.id}/file`)
      && response.request().method() === "PUT"
    ));
    await page.getByRole("button", { name: "Save", exact: true }).click();
    expect((await secondSaveResponse).ok()).toBeTruthy();
    await page.unroute(saveStatusFilePattern);
    await expect.poll(async () => {
      const savedLatestContent = await api.get(`/api/v1/documents/${saveStatusDocument.id}/content`, { headers });
      if (!savedLatestContent.ok()) return null;
      return (await savedLatestContent.json()).content;
    }).toBe(secondRevision);

    await page.goto(`/editor/${retryOrderDocument.id}`);
    const retryOrderEditor = page.getByPlaceholder("Start typing...", { exact: true });
    await expect(retryOrderEditor).toHaveValue("Initial retry order content.");
    let retryOrderAttempts = 0;
    let markFirstRetryAttempt = () => {};
    const firstRetryAttempt = new Promise<void>((resolve) => { markFirstRetryAttempt = resolve; });
    const retryOrderFilePattern = `**/documents/${retryOrderDocument.id}/file**`;
    await page.route(retryOrderFilePattern, async (route) => {
      if (route.request().method() !== "PUT") {
        await route.continue();
        return;
      }
      retryOrderAttempts += 1;
      if (retryOrderAttempts === 1) {
        markFirstRetryAttempt();
        await route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "Retry ordering probe" }) });
        return;
      }
      await route.continue();
    });
    const retryFirstRevision = "The first revision needs one retry.";
    const retryLatestRevision = "The latest revision must be persisted last.";
    await retryOrderEditor.fill(retryFirstRevision);
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await firstRetryAttempt;
    await retryOrderEditor.fill(retryLatestRevision);
    await page.keyboard.press("Control+s");
    await expect.poll(() => retryOrderAttempts).toBe(3);
    await page.unroute(retryOrderFilePattern);
    await expect.poll(async () => {
      const response = await api.get(`/api/v1/documents/${retryOrderDocument.id}/content`, { headers });
      return response.ok() ? (await response.json()).content : null;
    }).toBe(retryLatestRevision);

    await page.goto(`/editor/${richTextDocument.id}`);
    const insertMedia = page.getByRole("button", { name: "Insert media", exact: true });
    await expect(insertMedia).toBeVisible();
    await insertMedia.click();
    const mediaDialog = page.getByRole("dialog", { name: "Insert media", exact: true });
    await expect(mediaDialog).toBeVisible();
    await mediaDialog.getByRole("button", { name: "Close", exact: true }).click();
    await expect(mediaDialog).toBeHidden();
    await insertMedia.click();
    await expect(mediaDialog).toBeVisible();
    await page.waitForTimeout(500);

    expect(consoleErrors.filter((message) => /maximum update depth|too many re-renders/i.test(message))).toEqual([]);
  } finally {
    if (token) {
      for (const documentId of uploadedDocumentIds) {
        await api.post(`/api/v1/documents/${documentId}/trash`, {
          headers: { Authorization: `Bearer ${token}` },
        });
      }
    }
    await api.dispose();
  }
});

test("an auxiliary code tab saves through its entity-scoped filesystem path", async ({ page }) => {
  const api = await pwRequest.newContext({ baseURL: API });
  const suffix = Date.now();
  const uploadedDocumentIds: string[] = [];
  let token = "";

  try {
    const login = await api.post("/api/v1/auth/login", {
      data: { email: "demo@manor.local", password: "manor-demo" },
    });
    expect(login.ok(), `login failed: ${login.status()}`).toBeTruthy();
    token = (await login.json()).access_token;
    const headers = { Authorization: `Bearer ${token}` };
    const upload = async (name: string, mimeType: string, content: string) => {
      const response = await api.post("/api/v1/documents/upload", {
        headers,
        multipart: {
          file: { name, mimeType, buffer: Buffer.from(content, "utf8") },
        },
      });
      expect(response.ok(), `${name} upload failed: ${response.status()}`).toBeTruthy();
      const document = await response.json();
      uploadedDocumentIds.push(document.id);
      return document;
    };

    const htmlDocument = await upload(
      `code-project-${suffix}.html`,
      "text/html",
      "<!doctype html><link rel=\"stylesheet\" href=\"./styles.css\"><main>Project</main>",
    );
    const originalCss = "main { color: black; }";
    const cssDocument = await upload(
      `code-project-${suffix}.css`,
      "text/css",
      originalCss,
    );
    expect(cssDocument.fs_path).toBeTruthy();

    await page.addInitScript((accessToken) => {
      window.localStorage.setItem("manor_token", accessToken as string);
    }, token);
    await page.goto(`/editor/${htmlDocument.id}`);
    const rejectCookies = page.getByRole("button", { name: "Reject all", exact: true });
    if (await rejectCookies.isVisible()) await rejectCookies.click();
    const skipTour = page.getByRole("button", { name: "Skip", exact: true });
    if (await skipTour.isVisible()) await skipTour.click();

    const cssTreeRow = page.locator(".code-project-tree__file").filter({ hasText: cssDocument.name });
    await expect(cssTreeRow).toBeVisible();
    await cssTreeRow.click();
    const codeEditor = page.locator(".manor-editor-codearea");
    await expect(codeEditor).toHaveValue(originalCss);

    const updatedCss = "main { color: rebeccapurple; }";
    await codeEditor.fill(updatedCss);
    const writeResponse = page.waitForResponse((response) => {
      if (!response.url().endsWith("/api/v1/fs/write") || response.request().method() !== "POST") return false;
      const body = response.request().postDataJSON();
      return body.path === cssDocument.fs_path && body.content === updatedCss;
    });
    await page.getByRole("button", { name: "Save", exact: true }).click();
    const saved = await writeResponse;
    expect(saved.ok(), `auxiliary save failed: ${saved.status()}`).toBeTruthy();
    const writeBody = saved.request().postDataJSON();
    expect(writeBody.save_session_id).toEqual(expect.any(String));
    expect(writeBody.save_sequence).toEqual(expect.any(Number));

    const persisted = await api.get(
      `/api/v1/fs/read?path=${encodeURIComponent(cssDocument.fs_path)}`,
      { headers },
    );
    expect(persisted.ok()).toBeTruthy();
    expect((await persisted.json()).content).toBe(updatedCss);
  } finally {
    if (token) {
      for (const documentId of uploadedDocumentIds) {
        await api.post(`/api/v1/documents/${documentId}/trash`, {
          headers: { Authorization: `Bearer ${token}` },
        });
      }
    }
    await api.dispose();
  }
});
