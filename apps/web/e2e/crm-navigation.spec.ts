import { expect, test } from "@playwright/test";

const fixture = "/e2e/fixtures/crm.html";
const customer = /^Acme Demo /;

test("returning from profile editing closes the draft and leaves a blank add form", async ({ page }) => {
  await page.goto(fixture);
  await page.getByRole("button", { name: customer }).click();
  await page.getByRole("button", { name: "Edit profile", exact: true }).click();
  await expect(page.getByRole("textbox", { name: /^company \/ name$/i })).toHaveValue("Acme Demo");
  await page.goBack();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.getByRole("button", { name: "Add client", exact: true }).click();
  await expect(page.getByRole("textbox", { name: /^company \/ name$/i })).toHaveValue("");
});

for (const [section, action] of [
  ["Contacts", "Add contact"],
  ["Opportunities", "Add opportunity"],
  ["Follow-ups", "Add follow-up"],
]) {
  test(`returning from ${section} closes its editor`, async ({ page }) => {
    await page.goto(fixture);
    await page.getByRole("button", { name: customer }).click();
    await page.getByRole("tab", { name: section, exact: true }).click();
    await page.getByRole("button", { name: action, exact: true }).click();
    await expect(page.getByRole("dialog")).toHaveCount(1);
    await page.goBack();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.getByRole("tab", { name: "Profile", exact: true })).toHaveAttribute("aria-selected", "true");
  });
}

test("returning to the directory clears a pending archive confirmation", async ({ page }) => {
  await page.goto(fixture);
  await page.getByRole("button", { name: customer }).click();
  await page.getByRole("button", { name: "Archive client", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(1);
  await page.goBack();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("button", { name: customer })).toBeVisible();
});
