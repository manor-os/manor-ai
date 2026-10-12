import { expect, test, type JSHandle, type Locator, type Page } from "@playwright/test";

async function openFixture(page: Page) {
  await page.route("**/config", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", route => route.fulfill({ json: {} }));
  await page.goto("/e2e/fixtures/chat-attachments.html");
  await expect(page.getByRole("textbox")).toHaveText("Review these files");
}

async function fileTransfer(page: Page) {
  return page.evaluateHandle(() => {
    const transfer = new DataTransfer();
    transfer.items.add(new File(["First file contents"], "brief.txt", { type: "text/plain" }));
    transfer.items.add(new File(["Second file contents"], "notes.csv", { type: "text/csv" }));
    return transfer;
  });
}

async function drag(target: Locator, type: string, dataTransfer: JSHandle<DataTransfer>) {
  return target.evaluate((element, args) => {
    const event = new DragEvent(args.type, { bubbles: true, cancelable: true, dataTransfer: args.dataTransfer });
    element.dispatchEvent(event);
    return event.defaultPrevented;
  }, { type, dataTransfer });
}

test("dropped files append to picker attachments and retain the original File objects on send", async ({ page }) => {
  await openFixture(page);
  await page.screenshot({ path: "/tmp/manor-chat-default-desktop.png" });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: "/tmp/manor-chat-default-mobile.png" });
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.locator('input[type="file"]').setInputFiles({
    name: "existing.txt", mimeType: "text/plain", buffer: Buffer.from("Existing file"),
  });
  const transfer = await fileTransfer(page);
  const editor = page.getByRole("textbox");
  expect(await drag(editor, "dragenter", transfer)).toBe(true);
  await expect(page.locator(".chat-composer-drop-overlay")).toHaveText("Drop files to attach");
  expect(await drag(editor, "dragover", transfer)).toBe(true);
  expect(await drag(editor, "drop", transfer)).toBe(true);

  await expect(page.locator(".chat-composer-chip--file")).toHaveCount(3);
  await expect(editor).toHaveText("Review these files");
  await expect(page.getByTestId("draft")).toHaveText("Review these files");
  await expect(page.locator(".chat-composer-drop-overlay")).toHaveCount(0);
  await page.screenshot({ path: "/tmp/manor-chat-attached-desktop.png" });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: "/tmp/manor-chat-attached-mobile.png" });
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.getByRole("button", { name: "Remove notes.csv", exact: true }).click();
  await expect(page.locator(".chat-composer-chip--file")).toHaveCount(2);
  await page.locator(".chat-composer-send").click();

  const sent = await page.evaluate(async dataTransfer => {
    const sent = window.attachmentFixtureSent!;
    return {
      text: sent.text,
      names: sent.attachments.map(item => item.name),
      fileIsOriginal: sent.attachments[1].file === dataTransfer.files[0],
      contents: await sent.attachments[1].file!.text(),
      mimeType: sent.attachments[1].mimeType,
      type: sent.attachments[1].type,
    };
  }, transfer);
  expect(sent).toEqual({ text: "Review these files", names: ["existing.txt", "brief.txt"],
    fileIsOriginal: true, contents: "First file contents", mimeType: "text/plain", type: "file" });
  await expect(page.locator(".chat-composer-chip--file")).toHaveCount(0);
  await expect(editor).toBeEmpty();
});

test("file drag feedback survives child transitions, clears on exit, and ignores text drags", async ({ page }) => {
  await openFixture(page);
  const transfer = await fileTransfer(page);
  const composer = page.locator(".chat-composer");
  const editor = page.getByRole("textbox");
  const overlay = page.locator(".chat-composer-drop-overlay");
  await drag(composer, "dragenter", transfer);
  await drag(editor, "dragenter", transfer);
  await drag(editor, "dragleave", transfer);
  await expect(overlay).toBeVisible();
  await expect(overlay).toHaveAttribute("role", "status");
  await page.screenshot({ path: "/tmp/manor-chat-drop-desktop.png" });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(overlay).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: "/tmp/manor-chat-drop-mobile.png" });
  await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
  await page.screenshot({ path: "/tmp/manor-chat-drop-mobile-dark.png" });
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.screenshot({ path: "/tmp/manor-chat-drop-desktop-dark.png" });
  await drag(composer, "dragleave", transfer);
  await expect(overlay).toHaveCount(0);

  const textTransfer = await page.evaluateHandle(() => {
    const transfer = new DataTransfer();
    transfer.setData("text/plain", "Dragged text");
    return transfer;
  });
  for (const event of ["dragenter", "dragover", "drop"]) {
    expect(await drag(editor, event, textTransfer)).toBe(false);
  }
  await expect(overlay).toHaveCount(0);
  await expect(page.locator(".chat-composer-chip--file")).toHaveCount(0);
});

for (const gate of ["Disabled", "Streaming"]) {
  test(`${gate.toLowerCase()} composer rejects file drops and prevents native file navigation`, async ({ page }) => {
    await openFixture(page);
    const transfer = await fileTransfer(page);
    const composer = page.locator(".chat-composer");
    const overlay = page.locator(".chat-composer-drop-overlay");
    await drag(composer, "dragenter", transfer);
    await expect(overlay).toBeVisible();
    await page.getByLabel(gate, { exact: true }).check();
    await expect(overlay).toHaveCount(0);
    expect(await drag(composer, "dragover", transfer)).toBe(true);
    expect(await drag(composer, "drop", transfer)).toBe(true);
    await expect(page.locator(".chat-composer-chip--file")).toHaveCount(0);
    await expect(page.getByTestId("draft")).toHaveText("Review these files");
    await expect(page.getByRole("textbox")).toHaveAttribute("aria-disabled", "true");
  });
}
