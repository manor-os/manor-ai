import { expect, test, type Page, type Route, type TestInfo } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { PDFDocument, StandardFonts } from "pdf-lib";

// Presentation coverage only: customer responses are explicit UI fixtures, not
// evidence of real OTP, email delivery, legal acceptance, or PDF stamping.
test.skip(process.env.E2E_PDF_SIGNATURE !== "1", "Requires the isolated local signature stack");
test.describe.configure({ mode: "serial" });
test.use({ trace: "retain-on-failure", screenshot: "only-on-failure", actionTimeout: 15000 });

async function pdfFixture() {
  const pdf = await PDFDocument.create();
  const font = await pdf.embedFont(StandardFonts.Helvetica);
  for (let index = 1; index <= 2; index++) {
    const page = pdf.addPage([612, 792]);
    page.drawText("TEST ONLY - Signature UI preview", { x: 48, y: 735, size: 20, font });
    page.drawText(`Page ${index} of 2. Synthetic agreement; no legal acceptance.`, { x: 48, y: 700, size: 11, font });
  }
  return Buffer.from(await pdf.save());
}

async function capture(page: Page, info: TestInfo, name: string) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  if (name.startsWith("customer-")) {
    const brand = page.locator(".signature-brandbar").getByRole("img", { name: "Manor AI", exact: true });
    await expect(brand).toBeVisible();
    await expect(brand).toContainText("Manor AI");
    await expect(brand.locator("svg")).toHaveAttribute("width", "24");
    await expect(brand.locator("svg")).toHaveAttribute("height", "24");
    await expect(brand.locator("svg")).toHaveCSS("width", "24px");
    await expect(brand.locator("svg")).toHaveCSS("height", "24px");
    await expect(brand.locator("svg")).toHaveCSS("color", "rgb(11, 11, 11)");
    await expect(brand.locator("svg")).toHaveCSS("fill", "rgb(11, 11, 11)");
  }
  const path = info.outputPath(`${name}.png`);
  await page.screenshot({ path, fullPage: true });
  await info.attach(name, { path, contentType: "image/png" });
}

for (const mobile of [false, true]) {
  const viewport = mobile ? { width: 390, height: 844 } : { width: 1440, height: 1000 };
  const label = mobile ? "mobile" : "desktop";
  test(`${label}: live owner prepares and previews fields without sending`, async ({ page, request }, info) => {
    test.setTimeout(120000);
    await page.setViewportSize(viewport);
    const suffix = `${Date.now()}-${label}`;
    const email = `minimal-${suffix}@signature-e2e.test`;
    const password = "Minimal-UI-Test-Only!42";
    const registration = await request.post("/api/v1/auth/register", { data: {
      username: `minimal-${suffix}`, email, password, entity_name: "TEST ONLY - Minimal UI QA",
    } });
    expect(registration.ok(), await registration.text()).toBeTruthy();
    const headers = { Authorization: `Bearer ${(await registration.json()).access_token}` };
    const upload = await request.post("/api/v1/documents/upload", { headers, multipart: {
      file: { name: `TEST ONLY - Minimal UI ${suffix}.pdf`, mimeType: "application/pdf", buffer: await pdfFixture() },
    } });
    expect(upload.ok(), await upload.text()).toBeTruthy();
    const document = await upload.json();
    await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
    await page.goto("/login");
    await page.locator('input[type="email"]').fill(email);
    await page.locator('input[type="password"]').fill(password);
    await page.locator('button[type="submit"]').click();
    await expect.poll(() => page.evaluate(() => Boolean(localStorage.getItem("manor_token")))).toBeTruthy();
    await page.goto(`/viewer/${document.id}`);
    await page.getByRole("button", { name: "Send for signature", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Send for signature", exact: true });
    const next = dialog.getByRole("button", { name: "Preview and continue", exact: true });
    await expect(next).toBeDisabled();
    await expect(dialog.getByRole("button", { name: "Add Signature field", exact: true })).toBeEnabled();
    await capture(page, info, "owner-empty");
    await dialog.getByRole("button", { name: "Add Signature field", exact: true }).click();
    await dialog.getByRole("button", { name: "Place in page center", exact: true }).click();
    const fieldLabel = dialog.getByLabel("Field label", { exact: true });
    await fieldLabel.fill("Customer signature");
    await fieldLabel.scrollIntoViewIfNeeded();
    await fieldLabel.focus();
    await expect(fieldLabel).toBeFocused();
    await expect(fieldLabel).toBeInViewport({ ratio: 1 });
    expect(await fieldLabel.evaluate((node) => getComputedStyle(node).outlineWidth)).not.toBe("0px");
    await capture(page, info, "owner-selected-field");
    await next.scrollIntoViewIfNeeded();
    await expect(next).toBeInViewport();
    await next.click();
    await expect(dialog.getByRole("button", { name: "Send for signature", exact: true })).toBeDisabled();
    await dialog.getByLabel("Recipient name", { exact: true }).fill("TEST ONLY - Customer");
    const recipientEmail = dialog.getByLabel("Recipient emails", { exact: true });
    await recipientEmail.fill(`preview-${suffix}@signature-e2e.test`);
    await recipientEmail.scrollIntoViewIfNeeded();
    await recipientEmail.focus();
    await expect(recipientEmail).toBeFocused();
    await expect(recipientEmail).toBeInViewport({ ratio: 1 });
    if (mobile) expect(await dialog.getByTestId("signature-field-editor").locator("aside").evaluate((node) => node.clientHeight >= node.scrollHeight - 1)).toBeTruthy();
    const send = dialog.getByRole("button", { name: "Send for signature", exact: true });
    await expect(send).toBeEnabled();
    await send.scrollIntoViewIfNeeded();
    await expect(send).toBeInViewport({ ratio: 1 });
    await capture(page, info, "owner-preview-no-invitation-sent");
    const requests = await request.get(`/api/v1/documents/${document.id}/signature-requests`, { headers });
    expect(await requests.json()).toEqual([]);
  });

  test(`${label}: customer UI fixtures cover OTP, filling, completion and errors`, async ({ page }, info) => {
    test.setTimeout(120000);
    await page.setViewportSize(viewport);
    await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
    const token = `minimal-ui-${label}`;
    const prefix = `/api/v1/shared-doc/${token}/`;
    const pdf = await pdfFixture();
    let verified = false, completed = false, fail = false, loading = true;
    let held: Route | undefined;
    const fields = [
      { id: "signature", type: "signature", label: "Customer signature", required: true, page: 2, x: .15, y: .3, width: .45, height: .08 },
      { id: "reference", type: "text", label: "Project reference", required: true, page: 1, x: .15, y: .3, width: .65, height: .08 },
    ];
    await page.route((url) => url.pathname.startsWith(prefix), async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith("/signature-request")) {
        if (loading) { held = route; return; }
        if (fail) return route.fulfill({ status: 410, json: { detail: "This test request has expired." } });
        if (!verified) return route.fulfill({ status: 401, json: { detail: { code: "permissions.error.share.verification_required" } } });
        return route.fulfill({ json: { id: token, document_name: "TEST ONLY - Service agreement.pdf", recipient_name: "Customer QA", recipient_email: "customer@signature-e2e.test", sender_name: "Manor QA", message: "Review the two-page test copy and complete the marked fields.", status: completed ? "completed" : "pending", fields, placement: fields[0], signed_at: completed ? "2026-09-09T12:00:00Z" : null } });
      }
      if (path.endsWith("/request-otp")) return route.fulfill({ json: { status: "sent" } });
      if (path.endsWith("/verify-otp")) { verified = true; return route.fulfill({ json: { access_token: "ui-fixture-only" } }); }
      if (path.endsWith("/signature")) {
        const body = route.request().postDataJSON();
        expect(body.consent).toBe(true);
        expect(body.field_values.reference).toBe("TEST ONLY - Project reference");
        expect(body.field_values.signature).toMatch(/^data:image\/png;base64,/);
        completed = true;
        return route.fulfill({ json: { status: "completed", signed_at: "2026-09-09T12:00:00Z" } });
      }
      if (path.endsWith("/content") || path.endsWith("/download")) return route.fulfill({ contentType: "application/pdf", body: pdf });
      return route.abort();
    });
    await page.goto(`/sign/${token}`);
    await expect(page.getByRole("status")).toBeVisible();
    await capture(page, info, "customer-loading-ui-fixture");
    loading = false;
    await expect.poll(() => Boolean(held)).toBeTruthy();
    await held!.fulfill({ status: 401, json: { detail: { code: "permissions.error.share.verification_required" } } });
    await expect(page.getByRole("heading", { name: "Verify before signing" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Send code", exact: true })).toBeDisabled();
    await capture(page, info, "customer-email-gate-ui-fixture");
    await page.getByLabel("Email", { exact: true }).fill("customer@signature-e2e.test");
    await page.getByRole("button", { name: "Send code", exact: true }).click();
    await capture(page, info, "customer-code-gate-ui-fixture");
    await page.getByLabel("Verification code").fill("123456");
    await page.getByRole("button", { name: "Verify and open", exact: true }).click();
    await expect(page.getByRole("img", { name: "Page 2 of 2", exact: true })).toBeVisible();
    const submit = page.getByRole("button", { name: "Agree and sign", exact: true });
    await expect(submit).toBeDisabled();
    await capture(page, info, "customer-pending-ui-fixture");
    await page.getByTestId("signing-field-signature").click();
    const pad = page.getByLabel("Signature drawing area", { exact: true });
    await pad.scrollIntoViewIfNeeded();
    const box = (await pad.boundingBox())!;
    await page.mouse.move(box.x + 25, box.y + 30);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width - 25, box.y + 30, { steps: 8 });
    await page.mouse.up();
    await page.mouse.move(box.x + box.width / 2, box.y + 30);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width / 2, box.y + 95, { steps: 8 });
    await page.mouse.up();
    await capture(page, info, "customer-drawing-ui-fixture");
    await page.getByRole("button", { name: "Apply signature", exact: true }).click();
    await page.locator(".signature-field-list").getByRole("button", { name: /Project reference/ }).click();
    await page.getByTestId("signer-field-editor").getByLabel("Project reference").fill("TEST ONLY - Project reference");
    await expect(page.getByRole("img", { name: "Page 1 of 2", exact: true })).toBeVisible();
    await page.getByRole("checkbox", { name: /^I am / }).check();
    await expect(submit).toBeEnabled();
    await capture(page, info, "customer-filled-ui-fixture");
    await submit.click();
    await expect(page.getByRole("heading", { name: "Document signed", exact: true })).toBeVisible();
    await expect(page.getByRole("link", { name: "Download signed PDF" })).toBeVisible();
    await expect(page.getByText("Complete the highlighted fields in the document.", { exact: true })).toHaveCount(0);
    await capture(page, info, "customer-completed-ui-fixture");
    fail = true;
    await page.reload();
    await expect(page.getByRole("heading", { name: "This signature request is unavailable" })).toBeVisible();
    await expect(page.getByRole("alert")).toContainText("expired");
    await capture(page, info, "customer-error-ui-fixture");
  });

  test(`${label}: Manor branded email templates render without external requests`, async ({ page }, info) => {
    await page.setViewportSize(viewport);
    const appUrl = "https://manor-email-preview.test";
    const logo = await readFile("public/email-logo.png");
    const requests: string[] = [];
    await page.route(/^https?:/, (route) => {
      const url = route.request().url();
      requests.push(url);
      if (url === `${appUrl}/email-logo.png`) return route.fulfill({ contentType: "image/png", body: logo });
      return route.abort();
    });
    const template = await readFile("../../packages/core/templates/email/common.html", "utf8");
    await page.setContent(template.replaceAll("{{ app_url }}", appUrl).replace("{{ year }}", "2026").replace("{{ content }}", "<p><strong>Signature confirmed</strong></p><p>TEST ONLY - Service agreement.pdf</p><p>Your test document was signed by Customer QA.</p><p>This is a visual preview only. No email was sent and no agreement was signed.</p>"));
    await expect(page.getByText("Signature confirmed", { exact: true })).toBeVisible();
    await expect(page.getByRole("img", { name: "Manor AI", exact: true })).toHaveCount(1);
    await expect.poll(() => page.locator(".logo-icon").evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
    await expect(page.getByText("Empowering Progress, Simplifying Success")).toBeVisible();
    await expect(page.locator(".header-link")).toHaveAttribute("href", appUrl);
    await capture(page, info, "email-wrapper-ui-fixture");
    const verification = await readFile("../../packages/core/templates/email/verification.html", "utf8");
    await page.setContent(verification.replaceAll("{{ app_url }}", appUrl).replace("{{ year }}", "2026").replace("{{ code }}", "123456"));
    await expect(page.getByText("123456", { exact: true })).toBeVisible();
    await expect(page.getByText(/10 minutes/).first()).toBeVisible();
    await expect(page.getByRole("img", { name: "Manor AI", exact: true })).toHaveCount(1);
    await expect.poll(() => page.locator(".logo-icon").evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
    await expect(page.getByText(/never ask you for your verification code/)).toBeVisible();
    await capture(page, info, "verification-email-ui-fixture");
    expect(requests).toContain(`${appUrl}/email-logo.png`);
    expect(requests.every((url) => url === `${appUrl}/email-logo.png` || url.startsWith("https://fonts.googleapis.com/"))).toBeTruthy();
  });
}
