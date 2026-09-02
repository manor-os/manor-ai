import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, request as pwRequest, test } from "@playwright/test";
import JSZip from "jszip";

const API = process.env.E2E_API ?? "http://localhost:8000";
const repoRoot = fileURLToPath(new URL("../../../", import.meta.url));
test.skip(process.env.E2E_DOCKER_DOC_EDITOR !== "1", "Requires the live document API and local Python test dependencies");

function groupedTemplateGeneration(): Buffer {
  const script = String.raw`
import base64
from packages.core.ai.runtime.generated_files import _generate_office_operations_sync
from packages.core.contracts.file_engine import normalize_file_patch_operation
from tests.test_presentation_group_operations import _grouped_template

template, ids, _image = _grouped_template()
operations = [normalize_file_patch_operation(operation) for operation in [
    {"op": "text.set", "slide": 1, "shape_id": ids["nested_textbox"], "index": 0,
     "text": "Generated nested note"},
    {"op": "shape.format", "slide": 1, "shape_id": ids["card"],
     "format": {"fill_color": "DDEEFF", "line_color": "174C46"}},
    {"op": "shape.group", "slide": 1, "shape_ids": [ids["textbox"], ids["card"]]},
]]
result = _generate_office_operations_sync("pptx", operations, template_bytes=template)
assert result.get("patched"), result
print(base64.b64encode(result["_persisted_bytes"]).decode())
`;
  return Buffer.from(execFileSync(
    process.env.E2E_PYTHON ?? `${repoRoot}.venv/bin/python`,
    ["-c", script],
    { cwd: repoRoot, env: { ...process.env, PYTHONPATH: repoRoot }, encoding: "utf8", timeout: 30_000 },
  ).trim(), "base64");
}

function sourceObject(xml: string, objectId: string): string {
  const objects = [...xml.matchAll(/<p:(sp|pic|cxnSp|graphicFrame)\b[\s\S]*?<\/p:\1>/gi)].map((match) => match[0]);
  const object = objects.find((candidate) => new RegExp(`<p:cNvPr\\b[^>]*\\bid="${objectId}"`, "i").test(candidate));
  if (!object) throw new Error(`Missing source object ${objectId}`);
  return object;
}

test("grouped PPT template stays grouped and editable through generation, browser save, reload and presentation", async ({ page }, testInfo) => {
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
    const original = groupedTemplateGeneration();
    const before = await JSZip.loadAsync(original);
    const upload = await api.post("/api/v1/documents/upload", {
      headers,
      multipart: { file: {
        name: `native-groups-${Date.now()}.pptx`,
        mimeType: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
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
    const frame = page.locator(".presentation-editor-slide-frame");
    const groupedTitle = frame.getByRole("textbox", { name: "Grouped title", exact: true });
    await expect(groupedTitle).toBeVisible();
    await expect(frame.getByRole("textbox", { name: "Generated nested note", exact: true })).toBeVisible();
    await expect(frame.getByRole("textbox", { name: "Grouped card", exact: true })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("native-groups-before.png"), animations: "disabled" });

    await groupedTitle.click();
    await groupedTitle.press("F2");
    const input = frame.locator('[contenteditable="true"]');
    await expect(input).toBeFocused();
    await input.fill("Browser grouped title");
    await input.blur();
    await page.getByRole("button", { name: "Save", exact: true }).click();

    const part = "ppt/slides/slide1.xml";
    await expect.poll(async () => {
      const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
      if (!download.ok()) return "";
      return (await JSZip.loadAsync(await download.body())).file(part)!.async("string");
    }).toContain("Browser grouped title");
    const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
    const after = await JSZip.loadAsync(await download.body());
    const beforeXml = await before.file(part)!.async("string");
    const afterXml = await after.file(part)!.async("string");
    expect((afterXml.match(/<p:grpSp\b/gi) || []).length).toBe((beforeXml.match(/<p:grpSp\b/gi) || []).length);
    expect(afterXml.match(/<p:grpSpPr\b[\s\S]*?<\/p:grpSpPr>/i)?.[0]).toBe(
      beforeXml.match(/<p:grpSpPr\b[\s\S]*?<\/p:grpSpPr>/i)?.[0],
    );
    expect(sourceObject(afterXml, "4")).toBe(sourceObject(beforeXml, "4"));
    expect(sourceObject(afterXml, "10")).toBe(sourceObject(beforeXml, "10"));
    for (const id of ["2", "3", "4", "5", "6", "7", "8", "9", "10", "11"]) {
      expect(afterXml).toMatch(new RegExp(`<p:cNvPr\\b[^>]*\\bid="${id}"`, "i"));
    }
    for (const name of Object.keys(before.files).filter((entry) => (
      !before.files[entry].dir
      && ["ppt/charts/", "ppt/embeddings/", "ppt/slideMasters/"].some((prefix) => entry.startsWith(prefix))
    ))) {
      expect(await after.file(name)!.async("base64"), name).toBe(await before.file(name)!.async("base64"));
    }

    await page.reload();
    await expect(frame.getByRole("textbox", { name: "Browser grouped title", exact: true })).toBeVisible();
    await expect(frame.getByRole("textbox", { name: "Generated nested note", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Present", exact: true }).click();
    const present = page.locator(".presentation-editor-present-slide");
    await expect(present.getByText("Browser grouped title", { exact: true }).last()).toBeVisible();
    await expect(present.getByText("Generated nested note", { exact: true }).last()).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("native-groups-present.png"), animations: "disabled" });
    await page.keyboard.press("Escape");
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
