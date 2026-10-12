import { expect, request as pwRequest, test } from "@playwright/test";
import { readFile } from "node:fs/promises";

const API = process.env.E2E_API ?? "http://localhost:8000";
const RUN_DOCKER_E2E = process.env.E2E_DOCKER_DOC_EDITOR === "1";

test.skip(!RUN_DOCKER_E2E, "Set E2E_DOCKER_DOC_EDITOR=1 for the live DOCX editor test");

test("DOCX text editing autosaves and clears its saved confirmation", async ({ page }) => {
  const api = await pwRequest.newContext({ baseURL: API });
  let token = "";
  let documentId = "";

  try {
    const login = await api.post("/api/v1/auth/login", {
      data: { email: "demo@manor.local", password: "manor-demo" },
    });
    expect(login.ok(), `login failed: ${login.status()}`).toBeTruthy();
    token = (await login.json()).access_token;
    const headers = { Authorization: `Bearer ${token}` };
    const source = await readFile(new URL(
      "../public/assets/samples/artifacts/docs/shelter-photo-report.docx",
      import.meta.url,
    ));
    const name = `docx-edit-feedback-${Date.now()}.docx`;
    const upload = await api.post("/api/v1/documents/upload", {
      headers,
      multipart: {
        file: {
          name,
          mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          buffer: source,
        },
      },
    });
    expect(upload.ok(), `DOCX upload failed: ${upload.status()}`).toBeTruthy();
    documentId = (await upload.json()).id;

    await page.addInitScript((accessToken) => {
      window.localStorage.setItem("manor_token", accessToken as string);
    }, token);
    await page.goto(`/editor/${documentId}`);

    const rejectCookies = page.getByRole("button", { name: "Reject all", exact: true });
    if (await rejectCookies.isVisible()) await rejectCookies.click();
    const skipTour = page.getByRole("button", { name: "Skip", exact: true });
    if (await skipTour.isVisible()) await skipTour.click();

    await expect(page.locator(".manor-editor-header").getByText("Word document", { exact: true })).toBeVisible();
    await expect(page.locator(".manor-editor-header").getByText("DOCX", { exact: true })).toHaveCount(0);
    await expect(page.locator(".manor-editor-header").getByText("Word", { exact: true })).toHaveCount(0);
    await expect(page.getByText(/OOXML fidelity|fidelity mode|Lightweight edit/i)).toHaveCount(0);

    const editor = page.getByRole("textbox", { name: `${name} content`, exact: true });
    await expect(editor).toBeVisible();
    await expect(editor).toHaveAttribute("contenteditable", "true");
    await expect(page.getByTitle("Undo (Ctrl+Z)")).toBeEnabled();
    const toolsMenu = page.getByRole("button", { name: "Tools", exact: true });
    await expect(toolsMenu).toBeEnabled();
    await toolsMenu.click();
    await expect(page.getByRole("menuitem", { name: "Find", exact: true })).toBeEnabled();
    await toolsMenu.click();
    await expect(page.locator(".richtext-editor-toolbar")).not.toHaveAttribute("aria-disabled", "true");

    const savedStatus = page.locator(".manor-editor-header").getByText("Saved", { exact: true });
    await expect(savedStatus).toBeHidden();
    const editableParagraph = editor.locator("[data-docx-paragraph-index]").filter({ hasText: /\S/ }).first();
    await expect(editableParagraph).toBeVisible();
    const saveResponse = page.waitForResponse((response) => (
      response.url().includes(`/documents/${documentId}/file`)
      && response.request().method() === "PUT"
    ));
    await editableParagraph.click();
    await page.keyboard.press("End");
    await page.keyboard.type(" autosave check");
    await expect(page.locator(".manor-editor-header").getByText("Unsaved changes", { exact: true })).toBeVisible();
    expect((await saveResponse).ok()).toBeTruthy();
    await expect(savedStatus).toBeVisible();
    await expect(savedStatus).toBeHidden({ timeout: 4_000 });
  } finally {
    if (token && documentId) {
      await api.post(`/api/v1/documents/${documentId}/trash`, {
        headers: { Authorization: `Bearer ${token}` },
      });
    }
    await api.dispose();
  }
});
