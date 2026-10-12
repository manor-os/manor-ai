import { expect, test, type Locator, type Page, type TestInfo } from "@playwright/test";
import { PDFDocument, StandardFonts } from "pdf-lib";

// UI-only mobile fixtures: no real OTP, mail, signature submission or PDF write.
test.skip(process.env.E2E_PDF_SIGNATURE !== "1", "Requires the local signature web app");
test.describe.configure({ mode: "serial" });

async function sourcePdf() {
  const pdf = await PDFDocument.create();
  const font = await pdf.embedFont(StandardFonts.Helvetica);
  for (let number = 1; number <= 2; number++) {
    const page = pdf.addPage([612, 792]);
    page.drawText("TEST ONLY - Mobile signing UI", { x: 48, y: 730, size: 20, font });
    page.drawText(`Page ${number} of 2. This fixture does not sign an agreement.`, { x: 48, y: 695, size: 12, font });
  }
  return Buffer.from(await pdf.save());
}

async function touchTarget(control: Locator) {
  const box = (await control.boundingBox())!;
  expect(box.width).toBeGreaterThanOrEqual(44);
  expect(box.height).toBeGreaterThanOrEqual(44);
}

async function capture(page: Page, info: TestInfo, name: string) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  const logo = page.locator(".signature-brandbar").getByRole("img", { name: "Manor AI", exact: true });
  await expect(logo).toBeVisible();
  await expect(logo.locator("svg")).toHaveCSS("fill", "rgb(11, 11, 11)");
  const path = info.outputPath(`${name}.png`);
  await page.screenshot({ path, fullPage: true });
  await info.attach(name, { path, contentType: "image/png" });
}

const viewports = [
  { width: 320, height: 568 },
  { width: 390, height: 844 },
  { width: 844, height: 390 },
];

for (const viewport of viewports) {
  test(`${viewport.width}x${viewport.height}: mobile UI fixture supports touch, rotation and completion`, async ({ browser }, info) => {
    test.setTimeout(120000);
    const origin = process.env.E2E_BASE || `http://127.0.0.1:${process.env.E2E_PORT || 5186}`;
    const context = await browser.newContext({ baseURL: origin, viewport, isMobile: true, hasTouch: true, deviceScaleFactor: 2, reducedMotion: "reduce" });
    await context.addInitScript(() => localStorage.setItem("manor_locale", "en"));
    const page = await context.newPage();
    const token = `mobile-ui-fixture-${viewport.width}`;
    const prefix = `/api/v1/shared-doc/${token}/`;
    const pdf = await sourcePdf();
    let verified = false, completed = false, attempts = 0;
    const fields = [
      { id: "signature", type: "signature", label: "Customer signature", required: true, page: 2, x: .12, y: .3, width: .55, height: .09 },
      { id: "reference", type: "text", label: "Project reference", required: true, page: 1, x: .12, y: .3, width: .72, height: .09 },
    ];
    await page.route((url) => url.pathname.startsWith(prefix), async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith("/signature-request")) {
        if (!verified) return route.fulfill({ status: 401, json: { detail: { code: "permissions.error.share.verification_required" } } });
        return route.fulfill({ json: { id: token, document_name: "TEST ONLY - Mobile agreement.pdf", recipient_name: "Mobile QA", recipient_email: "mobile@signature-e2e.test", sender_name: "Manor QA", status: completed ? "completed" : "pending", fields, placement: fields[0], signed_at: completed ? "2026-09-09T12:00:00Z" : null } });
      }
      if (path.endsWith("/request-otp")) return route.fulfill({ json: { status: "sent" } });
      if (path.endsWith("/verify-otp")) { verified = true; return route.fulfill({ json: { access_token: "ui-fixture-only" } }); }
      if (path.endsWith("/signature")) {
        const body = route.request().postDataJSON();
        expect(body.consent).toBe(true);
        expect(body.field_values.reference).toBe("TEST ONLY - Mobile reference");
        expect(body.field_values.signature).toMatch(/^data:image\/png;base64,/);
        attempts += 1;
        if (attempts === 1) return route.fulfill({ status: 503, json: { detail: "UI fixture: please try again." } });
        completed = true;
        return route.fulfill({ json: { status: "completed", signed_at: "2026-09-09T12:00:00Z" } });
      }
      if (path.endsWith("/content") || path.endsWith("/download")) return route.fulfill({ contentType: "application/pdf", body: pdf });
      return route.abort();
    });
    try {
      await page.goto(`/sign/${token}`);
      const email = page.getByLabel("Email", { exact: true });
      await expect(email).toHaveCSS("font-size", "16px");
      const sendCode = page.getByRole("button", { name: "Send code", exact: true });
      await expect(sendCode).toBeDisabled();
      await touchTarget(sendCode);
      expect((await sendCode.boundingBox())!.width).toBeCloseTo((await page.locator(".signature-otp").boundingBox())!.width, 0);
      await capture(page, info, "mobile-otp-ui-fixture");
      await email.fill("mobile@signature-e2e.test");
      await sendCode.tap();
      const code = page.getByLabel("Verification code", { exact: true });
      await expect(code).toHaveCSS("font-size", "16px");
      await code.fill("123456");
      await page.getByRole("button", { name: "Verify and open", exact: true }).tap();
      await expect(page.getByRole("img", { name: "Page 2 of 2", exact: true })).toBeVisible();
      const progress = page.locator(".signature-mobile-progress");
      await expect(progress).toBeVisible();
      await touchTarget(progress.getByRole("button", { name: "Next required field", exact: true }));
      for (const button of await page.locator(".signature-field-list button").all()) await touchTarget(button);
      await touchTarget(page.getByRole("button", { name: "Previous page", exact: true }));
      await capture(page, info, "mobile-ready-ui-fixture");
      await progress.getByRole("button", { name: "Next required field", exact: true }).tap();
      const pad = page.getByLabel("Signature drawing area", { exact: true });
      await expect(pad).toHaveCSS("height", "160px");
      await pad.scrollIntoViewIfNeeded();
      const box = (await pad.boundingBox())!;
      const session = await context.newCDPSession(page);
      try {
        // Draw a synthetic T using genuine browser touch/pointer events.
        for (const stroke of [[[.15, .25], [.85, .25]], [[.5, .25], [.5, .8]]]) {
          for (const [index, [x, y]] of stroke.entries()) await session.send("Input.dispatchTouchEvent", { type: index ? "touchMove" : "touchStart", touchPoints: [{ x: box.x + box.width * x, y: box.y + box.height * y, id: 1 }] });
          await session.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
        }
      } finally { await session.detach(); }
      const apply = page.getByRole("button", { name: "Apply signature", exact: true });
      await expect(apply).toBeEnabled();
      const draft = await pad.evaluate((node) => (node as HTMLCanvasElement).toDataURL());
      await page.setViewportSize({ width: viewport.height, height: viewport.width });
      await expect.poll(async () => (await pad.boundingBox())!.width).not.toBe(box.width);
      await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
      await expect.poll(() => pad.evaluate((node) => (node as HTMLCanvasElement).toDataURL())).toBe(draft);
      await page.setViewportSize(viewport);
      await expect.poll(async () => (await pad.boundingBox())!.width).toBe(box.width);
      await expect(apply).toBeEnabled();
      await capture(page, info, "mobile-touch-draft-ui-fixture");
      await page.getByTestId("signer-field-editor").evaluate((node) => node.scrollIntoView({ block: "center", behavior: "instant" }));
      await expect(pad).toBeInViewport({ ratio: 1 });
      await expect(apply).toBeInViewport({ ratio: 1 });
      const drawingPath = info.outputPath("mobile-touch-draft-viewport.png");
      await page.screenshot({ path: drawingPath });
      await info.attach("mobile-touch-draft-viewport", { path: drawingPath, contentType: "image/png" });
      await apply.tap();
      await expect(page.getByTestId("signing-field-signature").locator("img")).toHaveAttribute("src", draft);
      await page.locator(".signature-field-list").getByRole("button", { name: /Project reference/ }).tap();
      const text = page.getByTestId("signer-field-editor").getByLabel("Project reference", { exact: true });
      await expect(text).toHaveCSS("font-size", "16px");
      await text.fill("T".repeat(2001));
      await expect(page.getByText("Use no more than 2,000 characters. Your text has not been shortened.")).toBeVisible();
      const submit = page.getByRole("button", { name: "Agree and sign", exact: true });
      await expect(submit).toBeDisabled();
      await text.fill("TEST ONLY - Mobile reference");
      await expect(submit).toBeDisabled();
      await page.getByRole("checkbox", { name: /^I am / }).check();
      await expect(submit).toBeEnabled();
      await touchTarget(submit);
      await submit.tap();
      await expect(page.getByRole("alert")).toHaveText("UI fixture: please try again.");
      await expect(submit).toBeEnabled();
      await capture(page, info, "mobile-submit-error-ui-fixture");
      await submit.tap();
      const completion = page.locator(".signature-completion");
      await expect(completion.getByRole("heading", { name: "Document signed", exact: true })).toBeVisible();
      const preview = page.locator(".signature-document-preview");
      await expect(preview.getByRole("img")).toBeVisible();
      expect((await completion.boundingBox())!.y).toBeLessThan((await preview.boundingBox())!.y);
      const download = completion.getByRole("link", { name: "Download signed PDF", exact: true });
      await download.scrollIntoViewIfNeeded();
      await expect(download).toBeInViewport({ ratio: 1 });
      await touchTarget(download);
      await capture(page, info, "mobile-completed-ui-fixture");
      expect(attempts).toBe(2);
    } finally { await context.close(); }
  });
}
