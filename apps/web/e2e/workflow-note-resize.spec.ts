import { expect, test } from "@playwright/test";

test("a note resizes from its corner and keeps the size it was given", async ({ page }) => {
  await page.goto("/e2e/fixtures/workflow-note.html");
  const note = page.locator(".react-flow__node-note");
  await expect(note).toBeVisible();
  const before = (await note.boundingBox())!;
  const grip = (await page.locator(".workflow-note-resize").boundingBox())!;
  // The grip sits inside the note, not on its edge.
  expect(grip.x + grip.width).toBeLessThanOrEqual(before.x + before.width);
  expect(grip.y + grip.height).toBeLessThanOrEqual(before.y + before.height);

  await page.mouse.move(grip.x + grip.width / 2, grip.y + grip.height / 2);
  await page.mouse.down();
  await page.mouse.move(grip.x + 80, grip.y + 60, { steps: 6 });
  await page.mouse.up();

  await expect.poll(async () => (await note.boundingBox())!.width).toBeGreaterThan(before.width + 40);
  expect((await note.boundingBox())!.height).toBeGreaterThan(before.height + 30);
  const saved = await page.evaluate(() => (window as any).__savedSteps?.[0]?.config);
  expect(saved.width).toBeGreaterThan(240);
  expect(saved.height).toBeGreaterThan(120);
  expect(saved.text).toBe("Remember this");
});
