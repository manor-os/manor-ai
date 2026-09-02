import { expect, test } from "@playwright/test";

const fixture = "/e2e/fixtures/integration-brand-catalog.html";

for (const width of [1280, 390]) {
  for (const theme of ["light", "dark"]) {
    test(`Google logos retain original colors at ${width}px in ${theme} mode`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await page.goto(fixture);
      if (theme === "dark") await page.getByRole("button", { name: "Toggle theme" }).click();
      const logos = page.getByRole("region", { name: "Google color logos" }).locator("img");
      await expect(logos).toHaveCount(15);
      await expect.poll(() => logos.evaluateAll(images => images.every(image => {
        const img = image as HTMLImageElement;
        return img.complete && img.naturalWidth > 0 && getComputedStyle(img).filter === "none";
      }))).toBe(true);
      for (const image of await logos.all()) {
        await expect(image).toHaveAttribute("aria-hidden", "true");
        await expect(image).toHaveAttribute("width", "36");
        await expect(image).toHaveAttribute("height", "36");
        await expect(image).not.toHaveAttribute("src", /^https?:/);
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      const themeButton = page.getByRole("button", { name: "Toggle theme" });
      await themeButton.focus();
      await page.keyboard.press("Tab");
      const gmailLink = page.getByRole("link", { name: "gmail", exact: true }).first();
      await expect(gmailLink).toBeFocused();
      await expect(gmailLink).toHaveAttribute("href", "/integrations?provider=gmail");
    });
  }
}

test("an unavailable logo keeps the integration name and setup destination usable", async ({ page }) => {
  await page.route("**/gmail.webp*", route => route.request().resourceType() === "image" ? route.abort() : route.continue());
  await page.goto(fixture);
  const gmailLink = page.getByRole("link", { name: "gmail", exact: true }).first();
  await expect(gmailLink).toBeVisible();
  await expect(gmailLink).toHaveAttribute("href", "/integrations?provider=gmail");
  await expect(gmailLink.locator("img")).toHaveCount(0);
  await expect(gmailLink.locator("[data-integration-brand=gmail] svg")).toBeVisible();
});
