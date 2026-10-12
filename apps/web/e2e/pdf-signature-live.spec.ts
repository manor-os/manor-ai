import { expect, test, type APIRequestContext, type Page, type TestInfo } from "@playwright/test";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { PDFDocument, PDFName, PDFString, StandardFonts, rgb } from "pdf-lib";

const LIVE = process.env.E2E_PDF_SIGNATURE === "1";
const INBOX = process.env.E2E_SIGNATURE_INBOX || "http://127.0.0.1:8026";
const SMTP_PORT = Number(process.env.E2E_SIGNATURE_SMTP_PORT || 1026);
const PASSWORD = "Signature-E2E-Only!42";
test.skip(!LIVE, "Start e2e/support/signature-server.py and set E2E_PDF_SIGNATURE=1");
test.describe.configure({ mode: "serial" });
test.use({ viewport: { width: 1440, height: 1000 }, trace: "retain-on-failure", actionTimeout: 15_000, screenshot: "only-on-failure" });

type Mail = { id: number; from: string; to: string[]; subject: string; content: string };

async function contract() {
  const pdf = await PDFDocument.create();
  const font = await pdf.embedFont(StandardFonts.Helvetica);
  for (let index = 0; index < 2; index++) {
    const page = pdf.addPage([612, 792]);
    page.drawText(index === 0 ? "CUSTOMER SERVICE AGREEMENT" : "ACCEPTANCE AND SIGNATURE", {
      x: 48, y: 724, size: 20, font, color: rgb(0.15, 0.14, 0.13),
    });
    const lines = index === 0
      ? ["Local browser E2E test contract", "Services: product implementation and review.", "Term: September 2026. Fee: USD 1,000.", "Read this page before accepting the agreement on page 2."]
      : ["I have reviewed both pages of this agreement.", "Please sign in the highlighted field below."];
    lines.forEach((line, row) => page.drawText(line, { x: 48, y: 666 - row * 30, size: 12, font }));
    page.drawText(`Page ${index + 1} of 2`, { x: 48, y: 36, size: 10, font });
  }
  return Buffer.from(await pdf.save({ useObjectStreams: false }));
}

async function setupOwner(page: Page, request: APIRequestContext, senderCount = 1) {
  const suffix = `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
  const email = `owner-${suffix}@signature-e2e.test`;
  const customer = `customer-${suffix}@signature-e2e.test`;
  const registered = await request.post("/api/v1/auth/register", {
    data: { username: `signature-${suffix}`, email, password: PASSWORD, entity_name: "PDF Signature Browser QA" },
  });
  expect(registered.ok(), await registered.text()).toBeTruthy();
  const token = (await registered.json()).access_token;
  expect(token).toBeTruthy();
  const headers = { Authorization: `Bearer ${token}` };
  const senderEmails = Array.from({ length: senderCount }, (_, index) => index === 0 ? email : `sender-${index}-${suffix}@signature-e2e.test`);
  for (const senderEmail of senderEmails) {
    const connected = await request.post("/api/v1/integrations", {
      headers,
      data: {
        provider: "email",
        credentials: {
          smtp_host: "127.0.0.1", smtp_port: SMTP_PORT,
          username: senderEmail, password: PASSWORD, from_address: senderEmail,
          use_tls_smtp: false, use_ssl_smtp: false,
        },
      },
    });
    expect(connected.status(), await connected.text()).toBe(201);
  }
  const source = await contract();
  const uploaded = await request.post("/api/v1/documents/upload", {
    headers,
    multipart: { file: { name: `Service agreement ${suffix}.pdf`, mimeType: "application/pdf", buffer: source } },
  });
  expect(uploaded.ok(), await uploaded.text()).toBeTruthy();
  const document = await uploaded.json();
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  await page.goto("/login");
  await page.locator('input[type="email"]').fill(email);
  await page.locator('input[type="password"]').fill(PASSWORD);
  const login = page.waitForResponse((response) => response.url().endsWith("/auth/login") && response.request().method() === "POST");
  await page.locator('button[type="submit"]').click();
  expect((await login).ok()).toBeTruthy();
  await expect.poll(() => page.evaluate(() => Boolean(localStorage.getItem("manor_token")))).toBeTruthy();
  await page.goto(`/viewer/${document.id}`);
  await expect(page.locator(".manor-editor-pdf-viewer-host canvas").first()).toBeVisible();
  await page.getByRole("button", { name: "Send for signature", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Send for signature", exact: true });
  await expect(dialog).toBeVisible();
  return { email, customer, headers, source, document, dialog, senderEmails };
}

async function createRequest(page: Page, customer: string, pageNumber = "2", senderEmail?: string, info?: TestInfo) {
  const dialog = page.getByRole("dialog", { name: "Send for signature", exact: true });
  const addSignature = dialog.getByRole("button", { name: "Add Signature field", exact: true });
  await expect(addSignature).toBeEnabled();
  const pageInput = dialog.getByLabel("PDF page number", { exact: true });
  await pageInput.fill(pageNumber);
  await pageInput.press("Enter");
  const sheet = dialog.getByTestId("signature-editor-page");
  await expect(sheet).toHaveAttribute("data-page-number", pageNumber);
  await expect(addSignature).toBeEnabled();
  await addSignature.click();
  const bounds = await sheet.boundingBox();
  expect(bounds).toBeTruthy();
  await sheet.click({ position: { x: bounds!.width * 0.3, y: bounds!.height * 0.3 } });
  await dialog.getByLabel("Field label", { exact: true }).fill("Customer signature");
  await dialog.getByLabel("Width (%)", { exact: true }).fill("30");
  await dialog.getByLabel("Height (%)", { exact: true }).fill("7");
  await dialog.getByLabel("Left (%)", { exact: true }).fill("62");
  await dialog.getByLabel("Top (%)", { exact: true }).fill("78");
  await expect(dialog.getByTestId("signature-editor-field")).toHaveCount(1);
  await expect(dialog.getByRole("button", { name: "Send for signature", exact: true })).toHaveCount(0);
  await dialog.getByRole("button", { name: "Preview and continue", exact: true }).click();
  await expect(dialog.getByRole("button", { name: "Edit fields", exact: true })).toBeVisible();
  await expect(addSignature).toHaveCount(0);
  await dialog.getByLabel("Recipient name", { exact: true }).fill("Alex Customer");
  await dialog.getByLabel("Recipient emails", { exact: true }).fill(customer);
  await dialog.getByLabel("Message (optional)").fill("Please review both pages, then sign the agreement.");
  if (senderEmail) {
    await expect(dialog.getByRole("button", { name: "Send for signature", exact: true })).toBeDisabled();
    await dialog.getByRole("button", { name: "Send from", exact: true }).click();
    await page.getByRole("option", { name: `${senderEmail} · Email`, exact: true }).click();
    if (info) {
      await screenshot(page, info, "sender-selected-before-send");
      await page.setViewportSize({ width: 390, height: 844 });
      await screenshot(page, info, "sender-selected-mobile");
      await page.setViewportSize({ width: 1440, height: 1000 });
    }
  }
  const response = page.waitForResponse((item) => /\/signature-requests$/.test(item.url()) && item.request().method() === "POST");
  await dialog.getByRole("button", { name: "Send for signature", exact: true }).click();
  return response;
}

test("sender account choices require a connection or an explicit selection", async ({ page, request }, info) => {
  const disconnected = await setupOwner(page, request, 0);
  await expect(disconnected.dialog.getByText("Connect an Email, Gmail, or Outlook account before sending this document for signature.")).toBeVisible();
  await screenshot(page, info, "no-email-connection");
  await page.setViewportSize({ width: 390, height: 844 });
  await screenshot(page, info, "no-email-connection-mobile");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await disconnected.dialog.getByRole("button", { name: "Connect email" }).click();
  await expect(page).toHaveURL(/\/integrations$/);

  const owner = await setupOwner(page, request, 2);
  const created = await createRequest(page, owner.customer, "2", owner.senderEmails[1], info);
  expect(created.status(), await created.text()).toBe(201);
  const invitation = await waitForMail(request, owner.customer, /^Signature requested:/);
  expect(invitation.from).toContain(owner.senderEmails[1]);
});

async function waitForMail(request: APIRequestContext, recipient: string, subject: RegExp, afterId = 0) {
  let found: Mail | undefined;
  await expect.poll(async () => {
    const response = await request.get(`${INBOX}/messages`);
    expect(response.ok()).toBeTruthy();
    found = ((await response.json()) as Mail[]).find((mail) => mail.id > afterId && mail.to.includes(recipient) && subject.test(mail.subject));
    return Boolean(found);
  }).toBeTruthy();
  return found!;
}

async function verifyRecipient(page: Page, request: APIRequestContext, url: string, email: string) {
  await page.goto(url);
  await expect(page.getByRole("heading", { name: "Verify before signing" })).toBeVisible();
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByRole("button", { name: "Send code", exact: true }).click();
  await expect(page.getByLabel("Verification code")).toBeVisible();
  const mail = await waitForMail(request, email, /verification|verify|code/i);
  const code = mail.content.match(/verification code is\s*<strong>(\d{6})<\/strong>/i)?.[1];
  expect(code, "SMTP inbox must receive a six-digit code").toBeTruthy();
  await page.getByLabel("Verification code").fill(code!);
  await page.getByRole("button", { name: "Verify and open" }).click();
  await expect(page.getByRole("button", { name: "Agree and sign" })).toBeVisible();
}

async function drawSignature(page: Page, touch = false) {
  const pad = page.getByLabel("Signature drawing area", { exact: true });
  await pad.scrollIntoViewIfNeeded();
  const box = await pad.boundingBox();
  expect(box).toBeTruthy();
  const points = [[0.10, 0.72], [0.24, 0.24], [0.33, 0.75], [0.52, 0.30], [0.61, 0.67], [0.9, 0.4]];
  if (touch) {
    const session = await page.context().newCDPSession(page);
    try {
      for (const [index, [x, y]] of points.entries()) {
        await session.send("Input.dispatchTouchEvent", {
          type: index === 0 ? "touchStart" : "touchMove",
          touchPoints: [{ x: box!.x + box!.width * x, y: box!.y + box!.height * y, id: 1 }],
        });
      }
      await session.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    } finally {
      await session.detach();
    }
  } else {
    await page.mouse.move(box!.x + box!.width * points[0][0], box!.y + box!.height * points[0][1]);
    await page.mouse.down();
    for (const [x, y] of points.slice(1)) {
      await page.mouse.move(box!.x + box!.width * x, box!.y + box!.height * y, { steps: 8 });
    }
    await page.mouse.up();
  }
  await expect(page.getByRole("button", { name: "Clear", exact: true })).toBeEnabled();
}

async function screenshot(page: Page, info: TestInfo, name: string) {
  const path = info.outputPath(`${name}.png`);
  await page.screenshot({ path, fullPage: true });
  await info.attach(name, { path, contentType: "image/png" });
}

for (const mobile of [false, true]) {
  test(`${mobile ? "mobile" : "desktop"}: real email, OTP, two-page review, drawing, signed PDF and owner status`, async ({ page, browser, request }, info) => {
    test.setTimeout(120_000);
    const owner = await setupOwner(page, request);
    const created = await createRequest(page, owner.customer);
    expect(created.status(), await created.text()).toBe(201);
    const signing = await created.json();
    expect(signing.delivery_status).toBe("sent");
    expect(signing.fields).toEqual([expect.objectContaining({
      type: "signature", label: "Customer signature", required: true,
      page: 2, x: 0.62, y: 0.78, width: 0.3, height: 0.07,
    })]);
    const invitation = await waitForMail(request, owner.customer, /^Signature requested:/);
    expect(invitation.content).toContain(signing.url);
    await expect(owner.dialog.getByText("Awaiting signature", { exact: true })).toBeVisible();
    await screenshot(page, info, "owner-request-sent");

    const context = await browser.newContext({
      viewport: mobile ? { width: 390, height: 844 } : { width: 1440, height: 1000 },
      isMobile: mobile, hasTouch: mobile,
    });
    await context.addInitScript(() => localStorage.setItem("manor_locale", "en"));
    const signer = await context.newPage();
    const pageErrors: string[] = [];
    signer.on("pageerror", (error) => pageErrors.push(error.message));
    try {
      const unverified = await context.request.get(signing.url.replace("/sign/", "/api/v1/shared-doc/") + "/content");
      expect(unverified.status()).toBe(401);
      await verifyRecipient(signer, request, signing.url, owner.customer);
      await expect(signer.getByRole("img", { name: "Page 2 of 2", exact: true })).toBeVisible();
      await signer.getByRole("button", { name: "Previous page", exact: true }).click();
      await expect(signer.getByRole("img", { name: "Page 1 of 2", exact: true })).toBeVisible();
      await expect(signer.getByRole("button", { name: "Edit Customer signature", exact: true })).toHaveCount(0);
      await signer.getByRole("button", { name: "Go to signature page", exact: true }).click();
      const signatureField = signer.getByRole("button", { name: "Edit Customer signature", exact: true });
      await expect(signatureField).toBeVisible();
      await expect(signer.getByRole("link", { name: "Download original PDF" })).toBeVisible();
      expect(await signer.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
      const submit = signer.getByRole("button", { name: "Agree and sign", exact: true });
      await expect(submit).toBeDisabled();
      await signatureField.click();
      const apply = signer.getByRole("button", { name: "Apply signature", exact: true });
      await expect(apply).toBeDisabled();
      await drawSignature(signer, mobile);
      await signer.getByRole("button", { name: "Clear", exact: true }).click();
      await expect(apply).toBeDisabled();
      await expect(submit).toBeDisabled();
      await drawSignature(signer, mobile);
      await expect(submit).toBeDisabled();
      await apply.click();
      await expect(signer.locator(".signature-sidebar").getByText("1 of 1 required fields complete", { exact: true })).toBeVisible();
      await expect(signatureField.getByRole("img", { name: "Customer signature", exact: true })).toBeVisible();
      await expect(submit).toBeDisabled();
      await signer.getByRole("checkbox", { name: /^I am / }).check();
      await expect(submit).toBeEnabled();
      await screenshot(signer, info, "customer-ready-to-sign");
      const completion = signer.waitForResponse((response) => response.url().endsWith("/signature") && response.request().method() === "POST");
      await submit.click();
      const completed = await completion;
      expect(completed.status(), await completed.text()).toBe(200);
      const receipt = await completed.json();
      await expect(signer.getByRole("heading", { name: "Document signed", exact: true })).toBeVisible();
      const downloadEvent = signer.waitForEvent("download");
      await signer.getByRole("link", { name: "Download signed PDF" }).click();
      const download = await downloadEvent;
      const signedPath = info.outputPath("signed-agreement.pdf");
      await download.saveAs(signedPath);
      const bytes = await readFile(signedPath);
      expect(createHash("sha256").update(bytes).digest("hex")).toBe(receipt.document_sha256);
      const signed = await PDFDocument.load(bytes);
      expect(signed.getPageCount()).toBe(2);
      const metadata = signed.context.lookup(signed.context.trailerInfo.Info) as any;
      expect((metadata.lookup(PDFName.of("ManorSigner"), PDFString) as PDFString).decodeText()).toBe(owner.customer);
      expect((metadata.lookup(PDFName.of("ManorSignatureStatus"), PDFString) as PDFString).decodeText()).toBe("completed");
      expect(signed.getPage(1).node.Resources()?.lookup(PDFName.of("XObject"))).toBeTruthy();
      await info.attach("signed-agreement", { path: signedPath, contentType: "application/pdf" });
      await signer.reload();
      await expect(signer.getByRole("heading", { name: "Document signed", exact: true })).toBeVisible();
      await expect(signer.getByRole("img", { name: "Page 2 of 2", exact: true })).toBeVisible();
      await screenshot(signer, info, "customer-completed");
      await waitForMail(request, owner.email, /^Signed:/);
      const duplicate = await context.request.post(signing.url.replace("/sign/", "/api/v1/shared-doc/") + "/signature", {
        data: completed.request().postDataJSON(),
      });
      expect(duplicate.status()).toBe(409);
      await page.keyboard.press("Escape");
      await page.getByRole("button", { name: "Send for signature", exact: true }).click();
      await expect(owner.dialog.getByText("Signed", { exact: true })).toBeVisible();
      await expect(owner.dialog.getByRole("button", { name: "Resend", exact: true })).toHaveCount(0);
      const logs = await request.get(`/api/v1/documents/${owner.document.id}/access-log`, { headers: owner.headers });
      const actions = (await logs.json()).map((row: any) => row.action);
      expect(actions).toEqual(expect.arrayContaining(["signature_request_create", "signature_request_view", "signature_complete"]));
      expect(actions.filter((action: string) => action === "signature_complete")).toHaveLength(1);
      const locked = await request.put(`/api/v1/documents/${owner.document.id}/file`, {
        headers: owner.headers, multipart: { file: { name: owner.document.name, mimeType: "application/pdf", buffer: owner.source } },
      });
      expect(locked.status()).toBe(409);
      expect(pageErrors).toEqual([]);
      await screenshot(page, info, "owner-completed");
    } finally {
      await context.close();
    }
  });
}

test("email failure remains visible; resend delivers a new link and revoke closes access", async ({ page, browser, request }, info) => {
  test.setTimeout(90_000);
  const owner = await setupOwner(page, request);
  await request.post(`${INBOX}/delivery-mode`, { data: { enabled: false } });
  let signing: any;
  try {
    const created = await createRequest(page, owner.customer);
    expect(created.status(), await created.text()).toBe(201);
    signing = await created.json();
    expect(signing.delivery_status).toBe("failed");
    await expect(owner.dialog.getByText("Delivery: Failed", { exact: true })).toBeVisible();
    await expect(owner.dialog.getByRole("button", { name: "Copy signing link" })).toBeVisible();
    await screenshot(page, info, "delivery-failure");
  } finally {
    await request.post(`${INBOX}/delivery-mode`, { data: { enabled: true } });
  }
  const resentResponse = page.waitForResponse((item) => item.url().endsWith("/resend") && item.request().method() === "POST");
  await owner.dialog.getByRole("button", { name: "Resend", exact: true }).click();
  const resent = await resentResponse;
  expect(resent.status(), await resent.text()).toBe(200);
  const renewed = await resent.json();
  expect(renewed.url).not.toBe(signing.url);
  expect(renewed.delivery_status).toBe("sent");
  const mail = await waitForMail(request, owner.customer, /^Signature requested:/);
  expect(mail.content).toContain(renewed.url);
  const context = await browser.newContext();
  await context.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  const guest = await context.newPage();
  try {
    await guest.goto(signing.url);
    await expect(guest.getByRole("heading", { name: "This signature request is unavailable" })).toBeVisible();
    await guest.goto(renewed.url);
    await expect(guest.getByRole("heading", { name: "Verify before signing" })).toBeVisible();
    const revoked = page.waitForResponse((item) => item.request().method() === "DELETE" && item.url().includes("/signature-requests/"));
    await owner.dialog.getByRole("button", { name: "Revoke", exact: true }).click();
    expect((await revoked).status()).toBe(204);
    await owner.dialog.locator("summary", { hasText: "Signature history" }).click();
    await expect(owner.dialog.getByText("Revoked", { exact: true })).toBeVisible();
    await guest.reload();
    await expect(guest.getByRole("heading", { name: "This signature request is unavailable" })).toBeVisible();
    await screenshot(guest, info, "revoked-link");
  } finally {
    await context.close();
  }
});

test("nonexistent signature page is rejected before invitation delivery", async ({ page, request }) => {
  const owner = await setupOwner(page, request);
  const pageInput = owner.dialog.getByLabel("PDF page number", { exact: true });
  await expect(owner.dialog.getByRole("button", { name: "Add Signature field", exact: true })).toBeEnabled();
  await pageInput.fill("99");
  await pageInput.press("Enter");
  await expect(pageInput).toHaveValue("1");
  await expect(owner.dialog.getByTestId("signature-editor-page")).toHaveAttribute("data-page-number", "1");
  await expect(owner.dialog.getByRole("button", { name: "Preview and continue", exact: true })).toBeDisabled();

  // The visual editor only exposes real pages; a forged API field must still fail server-side.
  const invalid = await request.post(`/api/v1/documents/${owner.document.id}/signature-requests`, {
    headers: owner.headers,
    data: {
      recipient_name: "Alex Customer", recipient_email: owner.customer,
      fields: [{ id: "invalid-page-signature", type: "signature", label: "Customer signature", required: true,
        page: 99, x: 0.3, y: 0.3, width: 0.3, height: 0.07 }],
    },
  });
  expect(invalid.status(), await invalid.text()).toBe(400);
  expect((await invalid.json()).detail).toBe("A field page is outside the PDF");
  const rows = await request.get(`/api/v1/documents/${owner.document.id}/signature-requests`, { headers: owner.headers });
  expect(await rows.json()).toEqual([]);
  const mail = await request.get(`${INBOX}/messages`);
  expect(((await mail.json()) as Mail[]).filter((item) => item.to.includes(owner.customer))).toEqual([]);
});
