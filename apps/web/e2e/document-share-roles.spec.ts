import { expect, test } from "@playwright/test";

for (const width of [1440, 390]) {
  test(`four sharing roles preserve legacy access and allow owner-equivalent control (${width}px)`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
    await page.route("**/api/v1/**", (route) => route.fulfill({ json: [] }));
    await page.goto("/e2e/fixtures/document-share-roles.html");
    await page.getByRole("button", { name: "Viewer", exact: true }).click();
    await expect(page.getByRole("option")).toHaveCount(4);
    await expect(page.getByRole("option", { name: /Access manager/ })).toHaveCount(0);
    await page.getByRole("option", { name: "Viewer", exact: true }).click();
    const editor = page.getByRole("button", { name: "Editor", exact: true });
    await expect(editor).toBeVisible();
    const legacy = JSON.parse(await page.getByTestId("selected-capabilities").textContent() || "[]");
    expect(legacy).toContain("grant_access");
    expect(legacy).not.toContain("share_external");
    await editor.click();
    await expect(page.getByRole("option")).toHaveCount(4);
    const coOwner = page.getByRole("option", { name: /Co-owner/ });
    await expect(coOwner).toContainText("Same permissions as the owner");
    await coOwner.click();
    await expect(page.getByRole("button", { name: "Co-owner", exact: true })).toBeVisible();
    const capabilities = JSON.parse(await page.getByTestId("selected-capabilities").textContent() || "[]");
    for (const cap of ["share_external", "delete", "grant_access", "reclassify", "download"]) expect(capabilities).toContain(cap);
    await page.screenshot({ path: info.outputPath("co-owner-selected.png"), fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.getByRole("button", { name: "Co-owner", exact: true }).click();
    await page.getByRole("option", { name: /Editor/ }).click();
    await expect(editor).toBeVisible();
    const downgraded = JSON.parse(await page.getByTestId("selected-capabilities").textContent() || "[]");
    expect(downgraded).not.toContain("share_external");
    expect(downgraded).not.toContain("delete");
    expect(downgraded).not.toContain("grant_access");
  });
}
