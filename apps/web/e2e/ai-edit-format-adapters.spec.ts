import {
  expect,
  request as pwRequest,
  test,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import { readFile } from "node:fs/promises";
import JSZip from "jszip";
import PptxGenJS from "pptxgenjs";
import * as XLSX from "xlsx";

const API = process.env.E2E_API ?? "http://localhost:8000";
const RUN_DOCKER_E2E = process.env.E2E_DOCKER_DOC_EDITOR === "1";
const SPREADSHEET_PAYLOAD_PREFIX = "__MANOR_SPREADSHEET_EDITOR_V1__\n";

type ApplyMeta = {
  complete: boolean;
  phase: "preview" | "complete";
  source: "assistant-stream";
  turnId: string;
};

type LiveEditAdapter = {
  read: () => string;
  beginTurn: (meta: ApplyMeta) => boolean | void | Promise<boolean | void>;
  preview: (content: string, meta: ApplyMeta) => boolean | void | Promise<boolean | void>;
  complete: (content: string, meta: ApplyMeta) => boolean | void | Promise<boolean | void>;
};

declare global {
  interface Window {
    __aiEditFormatAdapter?: LiveEditAdapter;
  }
}

test.skip(!RUN_DOCKER_E2E, "Set E2E_DOCKER_DOC_EDITOR=1 for live AI Edit format tests");
test.describe.configure({ mode: "serial" });

let api: APIRequestContext;
let token = "";
const uploadedDocumentIds: string[] = [];

test.beforeAll(async () => {
  api = await pwRequest.newContext({ baseURL: API });
  const login = await api.post("/api/v1/auth/login", {
    data: { email: "demo@manor.local", password: "manor-demo" },
  });
  expect(login.ok(), `login failed: ${login.status()}`).toBeTruthy();
  token = (await login.json()).access_token;
});

test.afterAll(async () => {
  try {
    for (const documentId of uploadedDocumentIds) {
      const response = await api.post(`/api/v1/documents/${documentId}/trash`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      expect(response.ok(), `cleanup failed for ${documentId}: ${response.status()}`).toBeTruthy();
    }
  } finally {
    await api.dispose();
  }
});

async function uploadDocument(name: string, mimeType: string, content: string | Buffer) {
  const response = await api.post("/api/v1/documents/upload", {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      file: {
        name,
        mimeType,
        buffer: Buffer.isBuffer(content) ? content : Buffer.from(content, "utf8"),
      },
    },
  });
  expect(response.ok(), `${name} upload failed: ${response.status()}`).toBeTruthy();
  const document = await response.json();
  uploadedDocumentIds.push(document.id);
  return document.id as string;
}

async function openEditor(page: Page, documentId: string) {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/login");
  await page.evaluate((accessToken) => {
    window.localStorage.setItem("manor_token", accessToken);
  }, token);
  await page.goto(`/editor/${documentId}`);
}

async function captureAiEditAdapter(page: Page) {
  await page.evaluate(() => {
    delete window.__aiEditFormatAdapter;
    window.addEventListener("manor:open-editor-live-chat", ((event: CustomEvent) => {
      window.__aiEditFormatAdapter = event.detail.adapter;
    }) as EventListener, { once: true });
  });
  // Audio keeps AI edit as its main action; PDF and image keep it in the menu beside Edit.
  const aiEdit = page.getByRole("button", { name: "AI edit", exact: true });
  if (await aiEdit.count()) await aiEdit.click();
  else {
    await page.getByRole("button", { name: "More", exact: true }).click();
    await page.getByRole("menuitem", { name: "AI edit", exact: true }).click();
  }
  await expect.poll(() => page.evaluate(() => typeof window.__aiEditFormatAdapter)).toBe("object");
}

async function readAiEditContent(page: Page) {
  return page.evaluate(() => {
    if (!window.__aiEditFormatAdapter) throw new Error("AI Edit adapter was not captured");
    return window.__aiEditFormatAdapter.read();
  });
}

async function driveAiEditPreview(page: Page, nextContent: string, turnId: string) {
  await page.evaluate(async ({ content, id }) => {
    const adapter = window.__aiEditFormatAdapter;
    if (!adapter) throw new Error("AI Edit adapter was not captured");
    adapter.read();
    await adapter.beginTurn({ complete: false, phase: "preview", source: "assistant-stream", turnId: id });
    const previewed = await adapter.preview(content, {
      complete: false,
      phase: "preview",
      source: "assistant-stream",
      turnId: id,
    });
    if (previewed === false) throw new Error("AI Edit preview was rejected");
    const completed = await adapter.complete(content, {
      complete: true,
      phase: "complete",
      source: "assistant-stream",
      turnId: id,
    });
    if (completed === false) throw new Error("AI Edit completion was rejected");
  }, { content: nextContent, id: turnId });
  await expect(aiEditPreviewControls(page)).toBeVisible();
}

function aiEditPreviewControls(page: Page) {
  return page.locator("#floating-chat-panel")
    .getByRole("group", { name: "AI edit preview controls" });
}

async function discardAiEditPreview(page: Page) {
  await aiEditPreviewControls(page).getByRole("button", { name: "Discard", exact: true }).click();
  await expect(page.locator(".doc-editor-live-preview-bar")).toHaveCount(0);
}

async function acceptAiEditPreview(page: Page) {
  await aiEditPreviewControls(page).getByRole("button", { name: "Accept", exact: true }).click();
  await expect(page.locator(".doc-editor-live-preview-bar")).toHaveCount(0);
  await expect(page.locator(".manor-editor-header").getByText("Saved", { exact: true })).toBeVisible();
}

test("Markdown AI Edit previews, discards, accepts, and preserves content beyond 500 characters", async ({ page }) => {
  test.setTimeout(90_000);
  const original = "# AI Edit Markdown\n\nOriginal markdown content.";
  const documentId = await uploadDocument(`ai-edit-${Date.now()}.md`, "text/markdown", original);
  await openEditor(page, documentId);
  const editor = page.getByPlaceholder("Write your markdown here...", { exact: true });
  await expect(editor).toHaveValue(original);
  await captureAiEditAdapter(page);

  const discarded = `${original}\n\nDiscard this live preview.`;
  await driveAiEditPreview(page, discarded, "markdown-discard");
  await expect(editor).toHaveValue(discarded);
  await discardAiEditPreview(page);
  await expect(editor).toHaveValue(original);

  const longParagraph = Array.from({ length: 80 }, (_, index) => `token-${index}`).join(" ");
  const accepted = `# Accepted Markdown\n\n${longParagraph}\n\nEnd marker: MARKDOWN_AI_EDIT_SAVED`;
  expect(accepted.length).toBeGreaterThan(500);
  await driveAiEditPreview(page, accepted, "markdown-accept");
  await expect(editor).toHaveValue(accepted);
  await acceptAiEditPreview(page);
  await page.reload();
  await expect(editor).toHaveValue(accepted);
});

test("Word AI Edit previews on canvas, discards, accepts, and persists native DOCX", async ({ page }) => {
  test.setTimeout(120_000);
  const source = await readFile(new URL(
    "../public/assets/samples/artifacts/docs/shelter-photo-report.docx",
    import.meta.url,
  ));
  const documentId = await uploadDocument(
    `ai-edit-${Date.now()}.docx`,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    source,
  );
  await openEditor(page, documentId);
  const editor = page.locator(".manor-docx-native[role='textbox']");
  await expect(editor).toBeVisible();
  await captureAiEditAdapter(page);

  const replaceFirstEditableParagraph = async (replacement: string) => page.evaluate((text) => {
    const adapter = window.__aiEditFormatAdapter;
    if (!adapter) throw new Error("AI Edit adapter was not captured");
    const container = document.createElement("div");
    container.innerHTML = adapter.read();
    const paragraph = Array.from(container.querySelectorAll<HTMLElement>(
      "p[data-docx-source-editable='true'], p[data-docx-paragraph-index], p",
    )).find((candidate) => Boolean(candidate.textContent?.trim()));
    if (!paragraph) throw new Error("Editable Word paragraph was not found");
    paragraph.textContent = text;
    return container.innerHTML;
  }, replacement);

  const discardedMarker = "WORD_AI_EDIT_DISCARDED";
  await driveAiEditPreview(page, await replaceFirstEditableParagraph(discardedMarker), "word-discard");
  await expect(editor.getByText(discardedMarker, { exact: true })).toBeVisible();
  await discardAiEditPreview(page);
  await expect(editor.getByText(discardedMarker, { exact: true })).toHaveCount(0);

  const acceptedMarker = "WORD_AI_EDIT_SAVED";
  await driveAiEditPreview(page, await replaceFirstEditableParagraph(acceptedMarker), "word-accept");
  await expect(editor.getByText(acceptedMarker, { exact: true })).toBeVisible();
  await acceptAiEditPreview(page);
  await page.reload();
  await expect(editor.getByText(acceptedMarker, { exact: true })).toBeVisible();

  const saved = await api.get(`/api/v1/documents/${documentId}/download`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(saved.ok()).toBeTruthy();
  const archive = await JSZip.loadAsync(await saved.body());
  expect(await archive.file("word/document.xml")!.async("text")).toContain(acceptedMarker);
});

test("Excel AI Edit previews on grid, discards, accepts, and persists native XLSX", async ({ page }) => {
  test.setTimeout(90_000);
  const workbook = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(workbook, XLSX.utils.aoa_to_sheet([["Original workbook cell"]]), "Sheet1");
  const documentId = await uploadDocument(
    `ai-edit-${Date.now()}.xlsx`,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    XLSX.write(workbook, { type: "buffer", bookType: "xlsx" }) as Buffer,
  );
  await openEditor(page, documentId);
  const formulaInput = page.locator(".manor-editor-main input").first();
  await expect(formulaInput).toHaveValue("Original workbook cell");
  await captureAiEditAdapter(page);

  const spreadsheetRevision = async (value: string) => {
    const current = await readAiEditContent(page);
    expect(current.startsWith(SPREADSHEET_PAYLOAD_PREFIX)).toBeTruthy();
    const payload = JSON.parse(current.slice(SPREADSHEET_PAYLOAD_PREFIX.length)) as {
      data: unknown[][];
    };
    payload.data[0][0] = value;
    return `${SPREADSHEET_PAYLOAD_PREFIX}${JSON.stringify(payload)}`;
  };

  await driveAiEditPreview(page, await spreadsheetRevision("EXCEL_AI_EDIT_DISCARDED"), "excel-discard");
  await expect(formulaInput).toHaveValue("EXCEL_AI_EDIT_DISCARDED");
  await discardAiEditPreview(page);
  await expect(formulaInput).toHaveValue("Original workbook cell");

  const acceptedMarker = "EXCEL_AI_EDIT_SAVED";
  await driveAiEditPreview(page, await spreadsheetRevision(acceptedMarker), "excel-accept");
  await expect(formulaInput).toHaveValue(acceptedMarker);
  await acceptAiEditPreview(page);
  await page.reload();
  await expect(formulaInput).toHaveValue(acceptedMarker);

  const saved = await api.get(`/api/v1/documents/${documentId}/download`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(saved.ok()).toBeTruthy();
  const savedWorkbook = XLSX.read(await saved.body(), { type: "buffer" });
  expect(savedWorkbook.Sheets[savedWorkbook.SheetNames[0]].A1?.v).toBe(acceptedMarker);
});

test("PowerPoint AI Edit previews on slide, discards, accepts, and persists native PPTX", async ({ page }) => {
  test.setTimeout(120_000);
  const presentation = new PptxGenJS();
  presentation.layout = "LAYOUT_WIDE";
  presentation.addSlide().addText("Original presentation text", { x: 1, y: 1, w: 6, h: 1 });
  const documentId = await uploadDocument(
    `ai-edit-${Date.now()}.pptx`,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    Buffer.from(await presentation.write({ outputType: "arraybuffer" }) as ArrayBuffer),
  );
  await openEditor(page, documentId);
  const editor = page.locator(".presentation-editor");
  await expect(editor).toBeVisible();
  await captureAiEditAdapter(page);

  const presentationRevision = async (replacement: string) => {
    const state = JSON.parse(await readAiEditContent(page)) as {
      slides: Array<{ shapes: Array<{ paragraphs?: Array<{ text: string; edits?: unknown[] }> }> }>;
    };
    const paragraph = state.slides.flatMap((slide) => slide.shapes)
      .flatMap((shape) => shape.paragraphs || [])[0];
    if (!paragraph) throw new Error("Editable presentation paragraph was not found");
    const originalText = paragraph.text;
    paragraph.text = replacement;
    paragraph.edits = [{ start: 0, end: originalText.length, text: replacement }];
    return JSON.stringify(state);
  };

  const discardedMarker = "PPT_AI_EDIT_DISCARDED";
  await driveAiEditPreview(page, await presentationRevision(discardedMarker), "ppt-discard");
  await expect(editor.getByRole("textbox", { name: discardedMarker, exact: true })).toBeVisible();
  await discardAiEditPreview(page);
  await expect(editor.getByRole("textbox", { name: discardedMarker, exact: true })).toHaveCount(0);

  const acceptedMarker = "PPT_AI_EDIT_SAVED";
  await driveAiEditPreview(page, await presentationRevision(acceptedMarker), "ppt-accept");
  await expect(editor.getByRole("textbox", { name: acceptedMarker, exact: true })).toBeVisible();
  await acceptAiEditPreview(page);
  await page.reload();
  await expect(editor.getByRole("textbox", { name: acceptedMarker, exact: true })).toBeVisible();

  const saved = await api.get(`/api/v1/documents/${documentId}/download`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(saved.ok()).toBeTruthy();
  const archive = await JSZip.loadAsync(await saved.body());
  const slideXml = await Promise.all(
    Object.keys(archive.files)
      .filter((path) => /^ppt\/slides\/slide\d+\.xml$/.test(path))
      .map((path) => archive.file(path)!.async("text")),
  );
  expect(slideXml.join("\n")).toContain(acceptedMarker);
});
