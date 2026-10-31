import { readFile } from "node:fs/promises";
import { expect, test, type Page } from "@playwright/test";
import JSZip from "jszip";

test.use({ timezoneId: "UTC" });

const createdAt = "2026-10-24T04:05:06Z";
const files = [
  { id: "one", name: "report.txt", body: "First report" },
  { id: "two", name: "report.txt", body: "Second report" },
  { id: "three", name: "notes.txt", body: "Notes" },
];
const metadata = (file: { id: string; name: string; body: string }) => ({
  id: file.id, name: file.name, size: Buffer.byteLength(file.body), mime_type: "text/plain",
  message_id: "human", sender_name: "Member", created_at: createdAt, can_delete: false,
});

async function prepare(page: Page, options: {
  paginated?: boolean;
  preparationFailure?: () => boolean;
  preparationWait?: Promise<void>;
  oversizeBatch?: boolean;
  artifactAttachment?: boolean;
  largePreviews?: boolean;
} = {}) {
  const downloaded: string[] = [];
  const previewRequests: string[] = [];
  const fileQueries: URLSearchParams[] = [];
  const errors: string[] = [];
  const tickets = new Map<string, { file_ids: string[]; archive: boolean }>();
  await page.addInitScript(() => {
    localStorage.setItem("manor_locale", "en");
    // Simulate the OS save dialog while exercising the real streamed save path.
    // Chromium's native attachment manager bypasses Playwright request routing.
    Object.defineProperty(window, "showSaveFilePicker", { configurable: true, value: async ({ suggestedName }: { suggestedName: string }) => ({
      createWritable: async () => {
        const chunks: Uint8Array[] = [];
        return new WritableStream<Uint8Array>({
          write(chunk) { chunks.push(chunk); },
          close() {
            const url = URL.createObjectURL(new Blob(chunks as BlobPart[]));
            const anchor = document.createElement("a"); anchor.href = url; anchor.download = suggestedName;
            document.body.appendChild(anchor); anchor.click(); anchor.remove();
            setTimeout(() => URL.revokeObjectURL(url), 60_000);
          },
        });
      },
    }) });
  });
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("https://fonts.googleapis.com/**", (route) => route.abort());
  await page.route("https://fonts.gstatic.com/**", (route) => route.abort());
  await page.route("**/config", (route) => route.fulfill({ json: {} }));
  await page.context().route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path.includes("/approval-settings/")) return route.fulfill({ json: {
      scope: { kind: "room", id: "group" }, policy: { mode: null, rules: [] },
      effective_policy: { mode: "risk_based", rules: [] }, inheritance: "following",
      root_scope: { kind: "account", id: "fixture" },
    } });
    if (/\/rooms\/(group|other)$/.test(path)) return route.fulfill({ json: {
      id: path.endsWith("other") ? "other" : "group", kind: "group", title: path.endsWith("other") ? "Other group" : "Study group",
      owner_scope: "personal", member_count: 1, members: [{ user_id: "member", name: "Member", role: "member" }],
      agents: [{ id: "assistant", name: "Assistant", available: true }], online_user_ids: [],
      can_manage_agents: false, can_manage_ai_execution: false, ai_tool_execution_enabled: false,
      ai_tool_execution_revision: 0, notifications_muted: false, last_message_at: createdAt, created_at: createdAt,
    } });
    if (path.endsWith("/messages")) return route.fulfill({ json: [
      { id: "human", room_id: "group", sender_user_id: "member", sender_name: "Member", content: "A human message", mentioned_user_ids: [], attachments: options.largePreviews ? [
        { ...metadata(files[0]), id: "image", name: "large.png", mime_type: "image/png", size: 101 * 1024 * 1024 },
        { ...metadata(files[0]), id: "video", name: "large.mp4", mime_type: "video/mp4", size: 101 * 1024 * 1024 },
        { ...metadata(files[0]), id: "audio", name: "large.mp3", mime_type: "audio/mpeg", size: 101 * 1024 * 1024 },
      ] : [], created_at: createdAt },
      { id: "ai", room_id: "group", sender_user_id: null, sender_name: "Assistant", sender_agent_binding_id: "assistant", sender_agent_name: "Assistant", content: "An AI reply", mentioned_user_ids: [], attachments: options.artifactAttachment ? [{ ...metadata(files[2]), size: options.largePreviews ? 1024 ** 3 : metadata(files[2]).size }] : [], created_at: createdAt },
    ] });
    if (path.endsWith("/retractions")) return route.fulfill({ json: { message_ids: [] } });
    if (path.endsWith("/presence")) return route.fulfill({ json: { online_user_ids: [] } });
    if (path.endsWith("/email-accounts")) return route.fulfill({ json: { accounts: [], default_sender_id: null } });
    if (path.endsWith("/files")) {
      fileQueries.push(url.searchParams);
      const list = options.paginated || options.oversizeBatch
        ? Array.from({ length: options.oversizeBatch ? 6 : 51 }, (_, index) => ({ id: `file-${index}`, name: `file-${index}.txt`, body: String(index) }))
        : files;
      const matching = list.filter((file) => file.name.includes(url.searchParams.get("q") || ""));
      const offset = url.searchParams.has("before") ? matching.findIndex((file) => file.id === url.searchParams.get("before")) + 1 : 0;
      return route.fulfill({ json: matching.slice(offset, offset + 50).map((file) => ({ ...metadata(file),
        size: options.oversizeBatch ? 1024 ** 3 : 300 * 1024 * 1024,
      })) });
    }
    if (/\/files\/[^/]+$/.test(path)) {
      previewRequests.push(path);
      return route.fulfill({ contentType: "text/plain", body: "Preview bytes" });
    }
    if (path.endsWith("/file-downloads")) {
      const selection = route.request().postDataJSON() as { file_ids: string[]; archive: boolean };
      downloaded.push(...selection.file_ids);
      if (options.preparationWait) await options.preparationWait;
      if (options.preparationFailure?.()) return route.fulfill({ status: 404, json: { detail: "File unavailable" } });
      const ticketId = `ticket-${tickets.size}`;
      tickets.set(ticketId, selection);
      return route.fulfill({ json: { download_url: `/api/v1/chat/rooms/group/file-downloads/${ticketId}` } }).catch(() => {});
    }
    const ticketMatch = path.match(/\/file-downloads\/([^/]+)$/);
    if (ticketMatch) {
      const selection = tickets.get(ticketMatch[1])!;
      if (!selection.archive) {
        const file = files.find((file) => file.id === selection.file_ids[0])!;
        return route.fulfill({ contentType: "text/plain", body: file.body, headers: { "Content-Disposition": `attachment; filename="${file.name}"` } });
      }
      const zip = new JSZip();
      const names = new Set<string>();
      for (const id of selection.file_ids) {
        const file = files.find((file) => file.id === id) || { name: `${id}.txt`, body: id };
        let name = file.name;
        if (names.has(name)) name = name.replace(/(\.[^.]+)$/, " (2)$1");
        names.add(name); zip.file(name, file.body);
      }
      return route.fulfill({ contentType: "application/zip", body: await zip.generateAsync({ type: "nodebuffer" }),
        headers: { "Content-Disposition": 'attachment; filename="Study group-files.zip"' } });
    }
    return route.fulfill({ json: [] });
  });
  await page.goto("/e2e/fixtures/group-file-batch.html");
  await expect(page.getByRole("heading", { name: "Study group" })).toBeVisible();
  return { downloaded, fileQueries, errors, previewRequests };
}

async function openFiles(page: Page) {
  await page.getByRole("tab", { name: "Files", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Group files", exact: true })).toBeVisible();
}

for (const width of [1280, 390]) {
  test(`selected files download in one intact ZIP at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 850 });
    const { downloaded, errors } = await prepare(page);
    await openFiles(page);
    await expect(page.getByRole("button", { name: "Download selected (0)", exact: true })).toBeDisabled();
    const selectors = page.getByRole("checkbox", { name: "Select report.txt", exact: true });
    await selectors.first().focus();
    await page.keyboard.press("Space");
    await expect(selectors.first()).toBeChecked();
    await selectors.nth(1).click();
    await expect(page.getByRole("button", { name: "Download selected (2)", exact: true })).toBeEnabled();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await page.screenshot({ path: testInfo.outputPath(`selected-${width}.png`) });
    const result = page.waitForEvent("download");
    await page.getByRole("button", { name: "Download selected (2)", exact: true }).click();
    const download = await result;
    expect(download.suggestedFilename()).toBe("Study group-files.zip");
    const zip = await JSZip.loadAsync(await readFile((await download.path())!));
    const entries = Object.values(zip.files).filter((file) => !file.dir);
    expect(entries).toHaveLength(2);
    expect(entries.map((entry) => entry.name)).toContain("report.txt");
    expect((await Promise.all(entries.map((entry) => entry.async("string")))).sort()).toEqual(["First report", "Second report"]);
    expect(downloaded).toEqual(["one", "two"]);
    expect(errors).toEqual([]);
  });
}

test("select loaded files respects pagination and search resets the selection", async ({ page }) => {
  const { downloaded, fileQueries } = await prepare(page, { paginated: true });
  await openFiles(page);
  await page.getByRole("checkbox", { name: "Select loaded files", exact: true }).click();
  await expect(page.getByRole("button", { name: "Download selected (50)", exact: true })).toBeEnabled();
  expect(fileQueries).toHaveLength(1);
  await page.getByRole("button", { name: "Load more", exact: true }).click();
  await expect(page.getByRole("checkbox", { name: "Select file-50.txt", exact: true })).toBeVisible();
  await expect(page.getByRole("checkbox", { name: "Select file-50.txt", exact: true })).not.toBeChecked();
  await page.getByRole("checkbox", { name: "Select loaded files", exact: true }).click();
  await expect(page.getByRole("button", { name: "Download selected (51)", exact: true })).toBeEnabled();
  await page.getByRole("textbox", { name: "Search group files", exact: true }).fill("file-50");
  await expect(page.getByRole("button", { name: "Download selected (0)", exact: true })).toBeDisabled();
  await expect(page.getByRole("checkbox", { name: "Select file-0.txt", exact: true })).toHaveCount(0);
  await expect(page.getByRole("checkbox", { name: "Select file-50.txt", exact: true })).not.toBeChecked();
  expect(downloaded).toEqual([]);
});

test("a failed file prevents a partial ZIP and keeps selection available for retry", async ({ page }) => {
  let fail = true;
  const downloads: string[] = [];
  page.on("download", (download) => downloads.push(download.suggestedFilename()));
  const { errors } = await prepare(page, { preparationFailure: () => fail });
  await openFiles(page);
  await page.getByRole("checkbox", { name: "Select loaded files", exact: true }).click();
  await page.getByRole("button", { name: "Download selected (3)", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Batch download failed:");
  await expect(page.getByRole("alert")).toContainText("File unavailable");
  await expect(page.getByRole("button", { name: "Download selected (3)", exact: true })).toBeEnabled();
  expect(downloads).toEqual([]);
  fail = false;
  const finished = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download selected (3)", exact: true }).click();
  await finished;
  expect(errors).toEqual([]);
});

for (const action of ["cancel", "switch rooms"] as const) {
  test(`${action} stops an unfinished ZIP download`, async ({ page }) => {
    let release!: () => void;
    const blocked = new Promise<void>((resolve) => { release = resolve; });
    const downloads: string[] = [];
    const failed: string[] = [];
    page.on("download", (download) => downloads.push(download.suggestedFilename()));
    page.on("requestfailed", (request) => { if (request.url().endsWith("/file-downloads")) failed.push(request.url()); });
    const { downloaded } = await prepare(page, { preparationWait: blocked });
    try {
      await openFiles(page);
      await page.getByRole("checkbox", { name: "Select loaded files", exact: true }).click();
      await page.getByRole("button", { name: "Download selected (3)", exact: true }).click();
      await expect.poll(() => downloaded).toEqual(["one", "two", "three"]);
      await expect(page.getByRole("status")).toContainText("Preparing download");
      if (action === "cancel") {
        await page.getByRole("button", { name: "Cancel", exact: true }).click();
        await expect(page.getByRole("button", { name: "Download selected (3)", exact: true })).toBeEnabled();
      } else {
        await page.getByRole("button", { name: "Switch fixture room", exact: true }).click();
        await expect(page.getByRole("heading", { name: "Other group", exact: true })).toBeVisible();
        await openFiles(page);
        await expect(page.getByRole("button", { name: "Download selected (0)", exact: true })).toBeDisabled();
      }
      await expect.poll(() => failed.length).toBe(1);
      release();
      expect(downloads).toEqual([]);
    } finally { release(); }
  });
}

test("human and AI timestamps share hover dates and keyboard expansion", async ({ page }) => {
  await prepare(page);
  for (const id of ["human", "ai"]) {
    const timestamp = page.locator(`#room-message-${id} .chat-timestamp-button`);
    await expect(timestamp).toHaveText("04:05 AM");
    await timestamp.hover();
    await expect(timestamp).toHaveAttribute("title", "Oct 24, 2026, 04:05:06 AM");
    await timestamp.focus();
    await expect(timestamp).toHaveAccessibleName(/Oct 24, 2026, 04:05:06 AM/);
    await timestamp.press("Enter");
    await expect(timestamp).toHaveAttribute("aria-expanded", "true");
    await expect(timestamp).toHaveText("Oct 24, 2026, 04:05:06 AM");
    await timestamp.click();
    await expect(timestamp).toHaveAttribute("aria-expanded", "false");
    await expect(timestamp).toHaveText("04:05 AM");
  }
});


test("a selection above 5 GiB is rejected before preparing a download", async ({ page }) => {
  const { downloaded } = await prepare(page, { oversizeBatch: true });
  await openFiles(page);
  await page.getByRole("checkbox", { name: "Select loaded files", exact: true }).click();
  await page.getByRole("button", { name: "Download selected (6)", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("5 GiB");
  expect(downloaded).toEqual([]);
});

test("single files stream to the save destination without the preview cache", async ({ page }) => {
  const { downloaded } = await prepare(page);
  await openFiles(page);
  const received = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download notes.txt", exact: true }).click();
  const download = await received;
  expect(download.suggestedFilename()).toBe("notes.txt");
  expect(await readFile((await download.path())!, "utf8")).toBe("Notes");
  expect(downloaded).toEqual(["three"]);
  await expect(page.getByRole("status")).toContainText("File saved.");
});

test("the composer accepts the 1 GiB boundary and rejects larger file metadata", async ({ page }) => {
  await prepare(page);
  await page.locator('input[type="file"]').evaluate((input: HTMLInputElement) => {
    // Exercise the UI boundary without allocating or uploading a gigabyte.
    const accepted = new File([], "at-limit.bin");
    const rejected = new File([], "too-large.bin");
    Object.defineProperty(accepted, "size", { value: 1024 ** 3 });
    Object.defineProperty(rejected, "size", { value: 1024 ** 3 + 1 });
    Object.defineProperty(input, "files", { value: [accepted, rejected], configurable: true });
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await expect(page.locator(".chat-composer-attachments")).toContainText("at-limit.bin");
  await expect(page.locator(".chat-composer-attachments")).not.toContainText("too-large.bin");
  await expect(page.getByRole("alert")).toHaveText("Group files must be 1 GiB or smaller");
});


test("group artifact cards share the streamed single-file download path", async ({ page }) => {
  const { downloaded } = await prepare(page, { artifactAttachment: true });
  const received = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download notes.txt", exact: true }).click();
  const download = await received;
  expect(await readFile((await download.path())!, "utf8")).toBe("Notes");
  expect(downloaded).toEqual(["three"]);
});


test("large group media and manual artifact previews do not fetch file bodies", async ({ page }) => {
  const { previewRequests, downloaded } = await prepare(page, { artifactAttachment: true, largePreviews: true });
  await expect(page.getByText("Large file. Download to view.", { exact: true })).toHaveCount(3);
  expect(previewRequests).toEqual([]);
  await page.getByRole("button", { name: "Open artifact: notes.txt", exact: true }).click();
  const panel = page.getByRole("complementary", { name: "Output panel" });
  await expect(panel.getByText("Large file. Download to view.", { exact: true })).toBeVisible();
  expect(previewRequests).toEqual([]);
  const received = page.waitForEvent("download");
  await panel.getByRole("button", { name: "Download", exact: true }).click();
  expect(await readFile((await (await received).path())!, "utf8")).toBe("Notes");
  expect(downloaded).toEqual(["three"]);
  await page.getByRole("button", { name: "Close artifact", exact: true }).click();
  await openFiles(page);
  await page.getByRole("button", { name: "Preview notes.txt", exact: true }).click();
  await expect(panel.getByText("Large file. Download to view.", { exact: true })).toBeVisible();
  expect(previewRequests).toEqual([]);
});
