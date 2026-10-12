import { expect, test } from "@playwright/test";
import { PDFDocument } from "pdf-lib";

// Real dialog and PDF rendering; isolated responses never send invitations.
for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  test(`decimal geometry allows preview after dragging and resizing (${viewport.width}px)`, async ({ page }, info) => {
    await page.setViewportSize(viewport);
    await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
    const pdf = await PDFDocument.create();
    pdf.addPage([612, 792]);
    const body = Buffer.from(await pdf.save());
    const invitations: string[] = [];
    await page.route("**/api/v1/**", (route) => {
      const path = new URL(route.request().url()).pathname;
      if (route.request().method() !== "GET") {
        invitations.push(path);
        return route.abort();
      }
      if (path.endsWith("/signature-senders")) return route.fulfill({ json: [
        { id: "fixture-sender", email: "sender@example.test", provider: "email", is_default: true },
      ] });
      if (path.endsWith("/signature-requests")) return route.fulfill({ json: [] });
      if (path.endsWith("/download")) return route.fulfill({ contentType: "application/pdf", body });
      return route.abort();
    });
    await page.goto("/e2e/fixtures/pdf-signature-dialog.html");
    const dialog = page.getByRole("dialog", { name: "Send for signature", exact: true });
    await dialog.getByRole("button", { name: "Add Signature field", exact: true }).click();
    await dialog.getByRole("button", { name: "Place in page center", exact: true }).click();
    const field = dialog.getByRole("button", { name: "Select Signature field", exact: true });
    await field.scrollIntoViewIfNeeded();
    const before = (await field.boundingBox())!;
    await page.mouse.move(before.x + before.width / 2, before.y + before.height / 2);
    await page.mouse.down();
    await page.mouse.move(before.x + before.width / 2 + 13, before.y + before.height / 2 + 7, { steps: 4 });
    await page.mouse.up();
    await expect.poll(async () => (await field.boundingBox())!.x).toBeGreaterThan(before.x + 10);
    const resize = dialog.getByRole("button", { name: "Resize Signature field", exact: true });
    await resize.scrollIntoViewIfNeeded();
    const handle = (await resize.boundingBox())!;
    const previousWidth = (await field.boundingBox())!.width;
    await page.mouse.move(handle.x + handle.width / 2, handle.y + handle.height / 2);
    await page.mouse.down();
    await page.mouse.move(handle.x + handle.width / 2 + 11, handle.y + handle.height / 2 + 5, { steps: 4 });
    await page.mouse.up();
    await expect.poll(async () => (await field.boundingBox())!.width).toBeGreaterThan(previousWidth + 8);
    const geometryLabels = ["Left (%)", "Top (%)", "Width (%)", "Height (%)"];
    for (const label of geometryLabels) {
      expect(await dialog.getByLabel(label, { exact: true }).evaluate((node: HTMLInputElement) => node.validity.valid), label).toBe(true);
    }
    // The precise values in the reported failure must also be submittable.
    for (const [index, value] of ["61.52", "21.5692", "21.4483", "2.8888"].entries()) {
      const input = dialog.getByLabel(geometryLabels[index], { exact: true });
      await input.fill(value);
      await input.press("Tab");
      await expect(input).toHaveValue(value);
      expect(await input.evaluate((node: HTMLInputElement) => node.validity.valid)).toBe(true);
    }
    const left = dialog.getByLabel("Left (%)", { exact: true });
    await left.press("ArrowUp");
    await expect(left).toHaveValue("61.62");
    await left.press("Shift+ArrowDown");
    await expect(left).toHaveValue("60.62");
    await left.press("Tab");
    await page.screenshot({ path: info.outputPath("decimal-geometry.png") });
    await dialog.getByRole("button", { name: "Preview and continue", exact: true }).click();
    await expect(dialog.getByLabel("Recipient emails", { exact: true })).toBeVisible();
    expect(invitations).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}
