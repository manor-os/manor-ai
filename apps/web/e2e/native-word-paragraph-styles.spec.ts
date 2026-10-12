import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, request as pwRequest, test } from "@playwright/test";
import JSZip from "jszip";

const API = process.env.E2E_API ?? "http://localhost:8000";
const repoRoot = fileURLToPath(new URL("../../../", import.meta.url));
test.skip(process.env.E2E_DOCKER_DOC_EDITOR !== "1", "Requires the live document API and local Python test dependencies");

function nativeStyledDocument(): Buffer {
  const script = String.raw`
import base64
from packages.core.ai.runtime.generated_files import _generate_office_operations_sync
from packages.core.contracts.file_engine import normalize_file_patch_operation
from tests.test_word_paragraph_style_operations import _style_operations

operations = [normalize_file_patch_operation(operation) for operation in _style_operations()]
result = _generate_office_operations_sync("docx", operations)
assert result.get("patched"), result
print(base64.b64encode(result["_persisted_bytes"]).decode())
`;
  return Buffer.from(execFileSync(
    process.env.E2E_PYTHON ?? `${repoRoot}.venv/bin/python`,
    ["-c", script],
    { cwd: repoRoot, env: { ...process.env, PYTHONPATH: repoRoot }, encoding: "utf8", timeout: 30_000 },
  ).trim(), "base64");
}

test("generated Word paragraph styles render and survive browser editing without rewriting styles.xml", async ({ page }, testInfo) => {
  test.setTimeout(90_000);
  const api = await pwRequest.newContext({ baseURL: API });
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let documentId = "";
  let token = "";
  try {
    const login = await api.post("/api/v1/auth/login", {
      data: { email: "demo@manor.local", password: "manor-demo" },
    });
    expect(login.ok()).toBeTruthy();
    token = (await login.json()).access_token;
    const headers = { Authorization: `Bearer ${token}` };
    const original = nativeStyledDocument();
    const before = await JSZip.loadAsync(original);
    const upload = await api.post("/api/v1/documents/upload", {
      headers,
      multipart: { file: {
        name: `native-word-styles-${Date.now()}.docx`,
        mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        buffer: original,
      } },
    });
    expect(upload.ok(), `upload failed: ${upload.status()} ${await upload.text()}`).toBeTruthy();
    documentId = (await upload.json()).id;

    await page.goto("/login");
    await page.evaluate((value) => localStorage.setItem("manor_token", value), token);
    await page.goto(`/editor/${documentId}`);
    const reject = page.getByRole("button", { name: "Reject all", exact: true });
    if (await reject.isVisible()) await reject.click();
    const editor = page.locator('.richtext-editor-workspace [contenteditable="true"]');
    const heading = editor.locator("p").filter({ hasText: /^Service proposal$/ });
    const body = editor.locator("p").filter({ hasText: /^Prepared for the client$/ });
    await expect(heading).toBeVisible();
    await expect(body).toBeVisible();
    const headingRun = heading.locator(":scope > span").first();
    await expect(headingRun).toHaveCSS("color", "rgb(23, 76, 70)");
    await expect(headingRun).toHaveCSS("font-family", /Arial/);
    expect(parseFloat(await headingRun.evaluate((element) => getComputedStyle(element).fontSize))).toBeGreaterThan(20);
    expect(parseInt(await headingRun.evaluate((element) => getComputedStyle(element).fontWeight), 10)).toBeGreaterThanOrEqual(700);
    await page.screenshot({ path: testInfo.outputPath("native-word-styles-before.png"), animations: "disabled" });

    await heading.fill("Browser styled proposal");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect.poll(async () => {
      const response = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
      if (!response.ok()) return "";
      return (await JSZip.loadAsync(await response.body())).file("word/document.xml")!.async("string");
    }).toContain("Browser styled proposal");
    const response = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
    const after = await JSZip.loadAsync(await response.body());
    expect(await after.file("word/styles.xml")!.async("base64")).toBe(
      await before.file("word/styles.xml")!.async("base64"),
    );
    const documentXml = await after.file("word/document.xml")!.async("string");
    expect(documentXml).toContain('<w:pStyle w:val="ClientHeading"');
    expect(documentXml).toContain('<w:pStyle w:val="ClientBody"');

    await page.reload();
    const reloaded = editor.locator("p").filter({ hasText: /^Browser styled proposal$/ });
    await expect(reloaded).toBeVisible();
    await expect(reloaded.locator(":scope > span").first()).toHaveCSS("color", "rgb(23, 76, 70)");
    await expect(reloaded.locator(":scope > span").first()).toHaveCSS("font-family", /Arial/);
    expect(errors).toEqual([]);
  } finally {
    if (documentId && token) {
      const cleanup = await api.post(`/api/v1/documents/${documentId}/trash`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      expect(cleanup.ok()).toBeTruthy();
    }
    await api.dispose();
  }
});
