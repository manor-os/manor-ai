import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";

test("misnamed HEIC uploads remain previewable and retry sends both real PNG files", async ({ page }) => {
  await page.goto("/e2e/fixtures/chat-upload-retry.html");
  await page.getByRole("button", { name: /IMG_9358.png/ }).click();
  const preview = page.locator(".chat-output-panel img").last();
  await expect(preview).toBeVisible();
  await expect.poll(() => preview.evaluate((image: HTMLImageElement) => image.naturalWidth)).toBe(96);
  await expect(page.getByText("2 个参考", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "重试", exact: true }).click();
  await expect(page.getByTestId("upload-receipt")).toContainText("收到 2 个文件");
  await expect(page.getByTestId("upload-receipt")).toContainText("IMG_9358.png: image/png, PNG=true");
  await expect(page.getByTestId("upload-receipt")).toContainText("IMG_9359.png: image/png, PNG=true");
});

test("downloaded local image retains PNG bytes and its filename", async ({ page }) => {
  await page.goto("/e2e/fixtures/chat-upload-retry.html?download");
  const downloadEvent = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载 IMG_9358.png", exact: true }).click();
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toBe("IMG_9358.png");
  const bytes = await readFile((await download.path())!);
  expect([...bytes.subarray(0, 8)]).toEqual([137, 80, 78, 71, 13, 10, 26, 10]);
});

test("decode failure leaves the draft and attachment available for retry", async ({ page }) => {
  await page.goto("/e2e/fixtures/chat-upload-retry.html?composer");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveText("无法处理图片，请重试或重新选择文件。");
  await expect(page.getByRole("textbox")).toContainText("转换这张图片");
  await expect(page.getByText("broken.heic", { exact: true })).toBeVisible();
  await expect(page.getByTestId("upload-receipt")).toHaveText("等待点击重试");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await page.getByRole("button", { name: "Remove broken.heic", exact: true }).click();
  await page.getByRole("button", { name: "换成有效图片", exact: true }).click();
  await expect(page.getByText("valid.heic", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByTestId("upload-receipt")).toContainText("valid.png: image/png, PNG=true");
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(page.getByRole("textbox")).toHaveText("");
});
