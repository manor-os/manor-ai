import { expect, test, type APIRequestContext, type Page, type TestInfo } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { PDFDocument, StandardFonts } from "pdf-lib";

const INBOX = process.env.E2E_SIGNATURE_INBOX || "http://127.0.0.1:8026";
const SMTP_PORT = Number(process.env.E2E_SIGNATURE_SMTP_PORT || 1026);
const FIELD_PAGE = Number(process.env.E2E_SIGNATURE_FIELD_PAGE || 2);
const SECOND_PAGE = Number(process.env.E2E_SIGNATURE_SECOND_PAGE || 1);
const PASSWORD = "Signature-Fields-E2E-Only!42";
test.skip(process.env.E2E_PDF_SIGNATURE !== "1", "Requires the local signature API and loopback SMTP inbox");
test.describe.configure({ mode: "serial" });
test.use({ viewport: { width: 1440, height: 1000 }, trace: "retain-on-failure", screenshot: "only-on-failure", actionTimeout: 15000 });

type FieldSpec = { type: string; label: string; page: number; x: number; y: number; width: number; height: number; value: string | boolean };
const FIELDS: FieldSpec[] = [
  { type: "Signature", label: "QA signature", page: FIELD_PAGE, x: .153, y: .163, width: .270, height: .039, value: "" },
  { type: "Name", label: "QA name", page: FIELD_PAGE, x: .174, y: .205, width: .251, height: .015, value: "TEST ONLY - Manor QA" },
  { type: "Text", label: "QA title", page: FIELD_PAGE, x: .166, y: .220, width: .259, height: .014, value: "TEST ONLY - QA" },
  { type: "Date", label: "QA date", page: FIELD_PAGE, x: .167, y: .234, width: .266, height: .015, value: "2026-09-09" },
  { type: "Checkbox", label: "QA acknowledgement", page: FIELD_PAGE, x: .153, y: .275, width: .020, height: .016, value: true },
  { type: "Text", label: "QA reference", page: SECOND_PAGE, x: .550, y: .303, width: .340, height: .020, value: "TEST ONLY - NOT A PROJECT" },
];

async function sourcePdf() {
  if (process.env.E2E_SIGNATURE_PDF) return readFile(process.env.E2E_SIGNATURE_PDF);
  const pdf = await PDFDocument.create();
  const font = await pdf.embedFont(StandardFonts.Helvetica);
  for (let i = 0; i < 2; i++) {
    const page = pdf.addPage([612, 792]);
    page.drawText(`TEST AGREEMENT PAGE ${i + 1}`, { x: 48, y: 740, size: 18, font });
    page.drawText("Original terms must survive signing.", { x: 48, y: 710, size: 12, font });
  }
  return Buffer.from(await pdf.save());
}

async function login(page: Page, request: APIRequestContext, suffix: string) {
  const email = `fields-owner-${suffix}@signature-e2e.test`;
  const registered = await request.post("/api/v1/auth/register", {
    data: { username: `fields-${suffix}`, email, password: PASSWORD, entity_name: "TEST ONLY - Signature Fields QA" },
  });
  expect(registered.ok(), await registered.text()).toBeTruthy();
  const token = (await registered.json()).access_token;
  const connected = await request.post("/api/v1/integrations", {
    headers: { Authorization: `Bearer ${token}` },
    data: {
      provider: "email",
      credentials: {
        smtp_host: "127.0.0.1", smtp_port: SMTP_PORT,
        username: email, password: PASSWORD, from_address: email,
        use_tls_smtp: false, use_ssl_smtp: false,
      },
    },
  });
  expect(connected.status(), await connected.text()).toBe(201);
  await page.addInitScript(() => {
    localStorage.setItem("manor_locale", "en");
    localStorage.setItem("manor-interface-mode", "professional");
    localStorage.setItem("manor_tour_completed", "true");
  });
  await page.goto("/login");
  await page.locator('input[type="email"]').fill(email);
  await page.locator('input[type="password"]').fill(PASSWORD);
  const response = page.waitForResponse((item) => item.url().endsWith("/auth/login") && item.request().method() === "POST");
  await page.locator('button[type="submit"]').click();
  expect((await response).ok()).toBeTruthy();
  await expect.poll(() => page.evaluate(() => Boolean(localStorage.getItem("manor_token")))).toBeTruthy();
  return email;
}

async function uploadThroughKnowledge(page: Page, name: string, mimeType: string, buffer: Buffer) {
  await page.goto("/knowledge");
  await page.getByLabel("Choose upload file", { exact: true }).setInputFiles({ name, mimeType, buffer });
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  const response = page.waitForResponse((item) => item.url().includes("/documents/upload") && item.request().method() === "POST");
  await dialog.getByRole("button", { name: /use defaults/i }).click();
  const uploaded = await response;
  expect(uploaded.status(), await uploaded.text()).toBe(201);
  const document = await uploaded.json();
  await expect(dialog).toBeHidden();
  return document;
}

async function selectPage(page: Page, number: number) {
  await expect(page.getByRole("button", { name: "Add Signature field", exact: true })).toBeEnabled();
  const input = page.getByLabel("PDF page number", { exact: true });
  await input.fill(String(number));
  await input.press("Enter");
  await expect(page.getByTestId("signature-editor-page")).toHaveAttribute("data-page-number", String(number));
  await expect(page.getByRole("button", { name: "Add Signature field", exact: true })).toBeEnabled();
}

async function placeField(page: Page, field: FieldSpec, exerciseDrag = false) {
  await selectPage(page, field.page);
  await page.getByRole("button", { name: `Add ${field.type} field`, exact: true }).click();
  const sheet = page.getByTestId("signature-editor-page");
  await sheet.click({ position: { x: (await sheet.boundingBox())!.width * .3, y: (await sheet.boundingBox())!.height * .3 } });
  await page.getByLabel("Field label", { exact: true }).fill(field.label);
  const added = page.getByRole("button", { name: `Select ${field.label} field`, exact: true });
  if (exerciseDrag) {
    const before = await added.boundingBox();
    await page.mouse.move(before!.x + before!.width / 2, before!.y + before!.height / 2);
    await page.mouse.down();
    await page.mouse.move(before!.x + before!.width / 2 + 24, before!.y + before!.height / 2 + 18, { steps: 8 });
    await page.mouse.up();
    await expect.poll(async () => (await added.boundingBox())!.x).toBeGreaterThan(before!.x + 10);
    const resize = page.getByRole("button", { name: `Resize ${field.label} field`, exact: true });
    const handle = await resize.boundingBox();
    const oldWidth = (await added.boundingBox())!.width;
    await page.mouse.move(handle!.x + handle!.width / 2, handle!.y + handle!.height / 2);
    await page.mouse.down();
    await page.mouse.move(handle!.x + handle!.width / 2 + 20, handle!.y + handle!.height / 2 + 8, { steps: 8 });
    await page.mouse.up();
    await expect.poll(async () => (await added.boundingBox())!.width).toBeGreaterThan(oldWidth + 10);
  }
  for (const [label, value] of [["Width (%)", field.width], ["Height (%)", field.height], ["Left (%)", field.x], ["Top (%)", field.y]] as const) {
    await page.getByLabel(label, { exact: true }).fill(String(value * 100));
    await page.getByLabel(label, { exact: true }).press("Tab");
  }
}

async function mail(request: APIRequestContext, email: string, subject: RegExp) {
  let result: any;
  await expect.poll(async () => {
    const response = await request.get(`${INBOX}/messages`);
    result = (await response.json()).find((item: any) => item.to.includes(email) && subject.test(item.subject));
    return Boolean(result);
  }).toBeTruthy();
  return result;
}

async function capture(page: Page, info: TestInfo, name: string) {
  const path = info.outputPath(`${name}.png`);
  await page.screenshot({ path, fullPage: true });
  await info.attach(name, { path, contentType: "image/png" });
}

async function drawTestSignature(page: Page, touch: boolean) {
  const pad = page.getByLabel("Signature drawing area", { exact: true });
  await pad.scrollIntoViewIfNeeded();
  const box = (await pad.boundingBox())!;
  // Deliberately write TEST, not a real person's signature.
  const strokes = [ [[.08,.25],[.24,.25]], [[.16,.25],[.16,.78]], [[.43,.25],[.29,.25],[.29,.78],[.43,.78]], [[.29,.5],[.41,.5]], [[.61,.25],[.48,.25],[.48,.5],[.61,.5],[.61,.78],[.48,.78]], [[.68,.25],[.84,.25]], [[.76,.25],[.76,.78]] ];
  const session = touch ? await page.context().newCDPSession(page) : undefined;
  for (const stroke of strokes) {
    for (const [index, [x, y]] of stroke.entries()) {
      const point = { x: box.x + box.width * x, y: box.y + box.height * y };
      if (session) await session.send("Input.dispatchTouchEvent", { type: index === 0 ? "touchStart" : "touchMove", touchPoints: [{ ...point, id: 1 }] });
      else {
        await page.mouse.move(point.x, point.y, { steps: index ? 4 : 1 });
        if (!index) await page.mouse.down();
      }
    }
    if (session) await session.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    else await page.mouse.up();
  }
  await session?.detach();
  await page.getByRole("button", { name: "Apply signature", exact: true }).click();
}

for (const mobile of [false, true]) {
  test(`${mobile ? "mobile touch" : "desktop"}: place and fill exact agreement fields across pages`, async ({ page, browser, request }, info) => {
    test.setTimeout(240000);
    const suffix = `${Date.now()}-${mobile ? "mobile" : "desktop"}`;
    const ownerEmail = await login(page, request, suffix);
    if (!mobile && process.env.E2E_SIGNATURE_DOCX) {
      await uploadThroughKnowledge(page, `TEST ONLY - Original agreement ${suffix}.docx`, "application/vnd.openxmlformats-officedocument.wordprocessingml.document", await readFile(process.env.E2E_SIGNATURE_DOCX));
    }
    const source = await sourcePdf();
    const original = await PDFDocument.load(source);
    const document = await uploadThroughKnowledge(page, `TEST ONLY - Agreement field QA ${suffix}.pdf`, "application/pdf", source);
    await page.goto(`/viewer/${document.id}`);
    await expect(page.locator(".manor-editor-pdf-viewer-host canvas").first()).toBeVisible();
    await page.getByRole("button", { name: "Send for signature", exact: true }).click();
    await expect(page.getByTestId("signature-field-editor")).toBeVisible();
    await expect(page.getByRole("button", { name: "Preview and continue" })).toBeDisabled();
    for (const [index, field] of FIELDS.entries()) await placeField(page, field, index === 0);
    await selectPage(page, FIELD_PAGE);
    await capture(page, info, "owner-placed-fields");
    await page.getByRole("button", { name: "Preview and continue" }).click();
    const customer = `fields-${suffix}@signature-e2e.test`;
    await page.getByLabel("Recipient name", { exact: true }).fill("TEST ONLY - Manor QA");
    await page.getByLabel("Recipient emails", { exact: true }).fill(customer);
    await page.getByLabel("Message (optional)").fill("TEST ONLY. Browser QA, not a real acceptance of this agreement.");
    await capture(page, info, "owner-review-before-send");
    const sent = page.waitForResponse((item) => /\/signature-requests$/.test(item.url()) && item.request().method() === "POST");
    await page.getByRole("button", { name: "Send for signature", exact: true }).last().click();
    const created = await sent;
    expect(created.status(), await created.text()).toBe(201);
    const signing = await created.json();
    expect(signing.fields).toHaveLength(FIELDS.length);
    for (const expected of FIELDS) {
      const actual = signing.fields.find((field: any) => field.label === expected.label);
      expect(actual.page).toBe(expected.page);
      for (const key of ["x", "y", "width", "height"] as const) expect(actual[key]).toBeCloseTo(expected[key], 3);
    }
    expect((await mail(request, customer, /^Signature requested:/)).content).toContain(signing.url);

    const context = await browser.newContext({ baseURL: new URL(signing.url).origin, viewport: mobile ? { width: 390, height: 844 } : { width: 1440, height: 1000 }, isMobile: mobile, hasTouch: mobile, acceptDownloads: true });
    await context.addInitScript(() => localStorage.setItem("manor_locale", "en"));
    const signer = await context.newPage();
    const errors: string[] = [];
    signer.on("pageerror", (error) => errors.push(error.message));
    try {
      await signer.goto(signing.url);
      await signer.getByLabel("Email", { exact: true }).fill(customer);
      await signer.getByRole("button", { name: "Send code", exact: true }).click();
      const code = (await mail(request, customer, /verification|verify|code/i)).content.match(/verification code is\s*<strong>(\d{6})<\/strong>/i)?.[1];
      expect(code, "SMTP inbox must receive a six-digit verification code").toBeTruthy();
      await signer.getByLabel("Verification code").fill(code!);
      await signer.getByRole("button", { name: "Verify and open" }).click();
      const submit = signer.getByRole("button", { name: "Agree and sign", exact: true });
      await expect(submit).toBeDisabled();
      const token = new URL(signing.url).pathname.split("/").at(-1);
      const missing = await context.request.post(`/api/v1/shared-doc/${token}/signature`, { data: { field_values: {}, consent: true } });
      expect(missing.status()).toBe(400);
      for (const field of FIELDS) {
        const saved = signing.fields.find((item: any) => item.label === field.label);
        if (field.page !== FIELD_PAGE) {
          await signer.getByRole("button", { name: new RegExp(field.label) }).first().click();
        }
        const overlay = signer.getByTestId(`signing-field-${saved.id}`);
        await expect(overlay).toBeVisible();
        await overlay.scrollIntoViewIfNeeded();
        await overlay.click();
        const editor = signer.getByTestId("signer-field-editor");
        if (field.type === "Signature") await drawTestSignature(signer, mobile);
        else if (field.type === "Checkbox") await editor.getByRole("checkbox").check();
        else {
          const input = editor.getByLabel(field.label, { exact: true });
          if (field.label === "QA reference") {
            await input.fill("T".repeat(650));
            await expect(input).toHaveValue("T".repeat(650));
            await input.fill("T".repeat(2001));
            await expect(input).toHaveValue("T".repeat(2001));
            await expect(editor.getByText("Use no more than 2,000 characters. Your text has not been shortened.")).toBeVisible();
            await expect(submit).toBeDisabled();
          }
          await input.fill(field.value as string);
        }
      }
      await capture(signer, info, "customer-filled-fields");
      await signer.getByRole("checkbox", { name: /I am .* and agree/ }).check();
      await expect(submit).toBeEnabled();
      const completedResponse = signer.waitForResponse((item) => item.url().endsWith("/signature") && item.request().method() === "POST");
      await submit.click();
      const completed = await completedResponse;
      expect(completed.status(), await completed.text()).toBe(200);
      const receipt = await completed.json();
      const senderConfirmation = await mail(request, ownerEmail, /^Signed:/);
      expect(senderConfirmation.subject).toBe(`Signed: ${document.name}`);
      expect(senderConfirmation.to).toEqual([ownerEmail]);
      expect(senderConfirmation.content).toContain(customer);
      await expect(signer.getByRole("heading", { name: "Document signed", exact: true })).toBeVisible();
      const downloadEvent = signer.waitForEvent("download");
      await signer.getByRole("link", { name: "Download signed PDF", exact: true }).click();
      const download = await downloadEvent;
      const output = info.outputPath("TEST-ONLY-signed-agreement.pdf");
      await download.saveAs(output);
      const signedBytes = await readFile(output);
      expect(createHash("sha256").update(signedBytes).digest("hex")).toBe(receipt.document_sha256);
      const signed = await PDFDocument.load(signedBytes);
      expect(signed.getPageCount()).toBe(original.getPageCount());
      expect(signedBytes.length).toBeGreaterThan(source.length);
      await info.attach("signed-test-copy", { path: output, contentType: "application/pdf" });
      await signer.reload();
      await expect(signer.getByRole("heading", { name: "Document signed", exact: true })).toBeVisible();
      await expect(signer.locator("main canvas").first()).toBeVisible();
      await capture(signer, info, "customer-completed");
      expect(errors).toEqual([]);
      expect(await signer.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
      await page.reload();
      await page.getByRole("button", { name: "Send for signature", exact: true }).click();
      await expect(page.getByRole("dialog").getByText("Signed", { exact: true }).first()).toBeVisible();
      await capture(page, info, "owner-signed-status");
      await info.attach("document-receipt", { body: JSON.stringify({ documentId: document.id, viewerPath: `/viewer/${document.id}`, fieldCount: signing.fields.length, pageCount: signed.getPageCount(), sha256: receipt.document_sha256 }), contentType: "application/json" });
    } finally { await context.close(); }
  });
}
