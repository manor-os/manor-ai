import { expect, test, type Page } from "@playwright/test";

async function openDelivery(page: Page, query = "") {
  await page.goto(`/e2e/fixtures/workspace-client-delivery.html?locale=en&crm=1${query}`);
  await page.getByRole("button", { name: "Deliver to a client", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("button", { name: "Send delivery invitation" })).toBeEnabled();
  return dialog;
}

async function submitAndRead(page: Page) {
  await page.getByRole("button", { name: "Send delivery invitation" }).click();
  await expect(page.getByRole("heading", { name: "Invitation email sent" })).toBeVisible();
  await page.getByRole("button", { name: "Close", exact: true }).click();
  return JSON.parse(await page.getByTestId("delivery-trace").innerText());
}

test("delivery copies exactly explicitly selected starter IDs and defaults to none", async ({ page }) => {
  const dialog = await openDelivery(page);
  await expect(dialog.getByRole("checkbox")).toHaveCount(3);
  await expect(dialog.getByRole("checkbox", { checked: true })).toHaveCount(0);
  await dialog.getByRole("checkbox", { name: "templates/template-2.csv", exact: true }).click();
  await page.screenshot({ path: test.info().outputPath("delivery-starters-desktop.png") });
  const result = await submitAndRead(page);
  expect(result.created).toBe(1);
  expect(result.trace[0].knowledge_document_ids).toEqual(["source-starter-2"]);
  await page.getByRole("button", { name: "Deliver to a client", exact: true }).click();
  await expect(page.getByRole("checkbox", { checked: true })).toHaveCount(0);
  const second = await submitAndRead(page);
  expect(second.trace[1].knowledge_document_ids).toEqual([]);
});

test("selection survives pagination, caps at 64 and clears when the source changes", async ({ page }) => {
  const dialog = await openDelivery(page, "&many_starters=1");
  for (let i = 1; i <= 50; i++) await dialog.getByRole("checkbox", { name: `templates/template-${i}.csv`, exact: true }).click();
  await dialog.getByRole("button", { name: "Next files" }).click();
  for (let i = 51; i <= 64; i++) await dialog.getByRole("checkbox", { name: `templates/template-${i}.csv`, exact: true }).click();
  await expect(dialog.getByText("64 of 64 files selected", { exact: true })).toBeVisible();
  await expect(dialog.getByRole("checkbox", { name: "templates/template-65.csv", exact: true })).toBeDisabled();
  await dialog.getByRole("button", { name: "Previous files" }).click();
  await expect(dialog.getByRole("checkbox", { checked: true })).toHaveCount(50);
  await dialog.getByRole("button", { name: "Choose a Workspace" }).click();
  await page.getByRole("option", { name: "Second Workspace", exact: true }).click();
  await expect(dialog.getByText("0 of 64 files selected", { exact: true })).toBeVisible();
  await expect(dialog.getByRole("checkbox", { checked: true })).toHaveCount(0);
});

test("failed starter lookup can retry without selecting files or creating an invitation", async ({ page }) => {
  const dialog = await openDelivery(page, "&fault=knowledge");
  await expect(dialog.getByRole("alert")).toContainText("Could not load starter files.");
  await dialog.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(dialog.getByRole("checkbox")).toHaveCount(3);
  await expect(dialog.getByRole("checkbox", { checked: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Close", exact: true }).click();
  expect(JSON.parse(await page.getByTestId("delivery-trace").innerText()).created).toBe(0);
});

test("narrow layout supports keyboard selection and an empty candidate list", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const dialog = await openDelivery(page);
  const file = dialog.getByRole("checkbox", { name: "templates/template-1.csv", exact: true });
  await file.focus();
  await page.keyboard.press("Space");
  await expect(file).toBeChecked();
  const box = await dialog.boundingBox();
  expect(box!.width).toBeLessThanOrEqual(390);
  expect(box!.x).toBeGreaterThanOrEqual(0);
  await page.screenshot({ path: test.info().outputPath("delivery-starters-mobile.png") });
  await page.goto("/e2e/fixtures/workspace-client-delivery.html?locale=en&crm=1&no_starters=1");
  await page.getByRole("button", { name: "Deliver to a client", exact: true }).click();
  await expect(page.getByText("No eligible starter files.", { exact: true })).toBeVisible();
  expect((await submitAndRead(page)).trace[0].knowledge_document_ids).toEqual([]);
});


test("changing the signed-in company clears starter selection while the dialog stays open", async ({ page }) => {
  const dialog = await openDelivery(page);
  await dialog.getByRole("checkbox", { name: "templates/template-2.csv", exact: true }).click();
  await expect(dialog.getByRole("checkbox", { checked: true })).toHaveCount(1);
  await page.evaluate(() => window.dispatchEvent(new Event("fixture-account-change")));
  await expect(dialog.getByRole("checkbox", { checked: true })).toHaveCount(0);
  await expect(dialog.getByText("0 of 64 files selected", { exact: true })).toBeVisible();
  expect((await submitAndRead(page)).trace[0].knowledge_document_ids).toEqual([]);
});
