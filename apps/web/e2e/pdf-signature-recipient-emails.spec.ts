import { expect, test } from "@playwright/test";
import { PDFDocument } from "pdf-lib";

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  test(`one signer can receive the invitation at multiple emails (${viewport.width}px)`, async ({ page }, info) => {
    await page.setViewportSize(viewport);
    await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
    const pdf = await PDFDocument.create();
    pdf.addPage([612, 792]);
    const body = Buffer.from(await pdf.save());
    const payloads: any[] = [];
    let invitations: any[] = [];
    // Every request stays in this fixture; no invitation leaves the browser.
    await page.route("**/api/v1/**", (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      if (path.endsWith("/signature-senders")) return route.fulfill({ json: [
        { id: "fixture-sender", email: "sender@example.test", provider: "email", is_default: true },
      ] });
      if (path.endsWith("/signature-requests")) {
        if (request.method() === "POST") {
          const payload = request.postDataJSON();
          payloads.push(payload);
          invitations = [{ ...payload, id: "single-request", status: "pending", delivery_status: "sent",
            placement: payload.fields[0], group_size: 1, group_finalized: false }];
          return route.fulfill({ json: invitations[0], status: 201 });
        }
        return route.fulfill({ json: invitations });
      }
      if (path.endsWith("/download")) return route.fulfill({ contentType: "application/pdf", body });
      return route.abort();
    });
    await page.goto("/e2e/fixtures/pdf-signature-dialog.html");
    const dialog = page.getByRole("dialog", { name: "Send for signature", exact: true });
    await dialog.getByRole("button", { name: "Add Signature field", exact: true }).click();
    await dialog.getByRole("button", { name: "Place in page center", exact: true }).click();
    await dialog.getByRole("button", { name: "Preview and continue", exact: true }).click();
    await dialog.getByLabel("Recipient name", { exact: true }).fill("One signer");
    const emails = dialog.getByLabel("Recipient emails", { exact: true });
    await expect(emails).toHaveAttribute("multiple", "");
    await emails.fill("invalid-address, alternate@example.test");
    await dialog.getByRole("button", { name: "Send for signature", exact: true }).click();
    expect(payloads).toEqual([]);
    await emails.fill("PRIMARY@example.test, alternate@example.test, primary@example.test");
    expect(await emails.evaluate((node: HTMLInputElement) => node.validity.valid)).toBe(true);
    await page.screenshot({ path: info.outputPath("multiple-recipient-emails.png") });
    await dialog.getByRole("button", { name: "Send for signature", exact: true }).click();
    await expect.poll(() => payloads.length).toBe(1);
    expect(payloads[0].recipient_email).toBe("primary@example.test");
    expect(payloads[0].recipient_emails).toEqual(["primary@example.test", "alternate@example.test"]);
    expect(payloads[0].additional_recipients).toEqual([]);
    expect(payloads[0].fields).toHaveLength(1);
    await expect(dialog.getByText("primary@example.test, alternate@example.test", { exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}
