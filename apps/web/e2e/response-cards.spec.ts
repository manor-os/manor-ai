import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.route("**/api/**", (route) => route.fulfill({ status: 404, contentType: "application/json", body: "{}" }));
  await page.route("https://tile.openstreetmap.org/**", (route) => route.fulfill({ status: 200, contentType: "image/png", body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==", "base64") }));
  await page.goto("/e2e/fixtures/response-cards.html");
});

test("reply cards draw in the page, without a frame or an action button", async ({ page }) => {
  await expect(page.locator("section.response-card").first()).toBeVisible();
  await expect(page.locator("iframe")).toHaveCount(0);
  await expect(page.locator('[data-card="card.delivery"]').getByRole("link", { name: /TikTok/ })).toHaveAttribute("href", "https://example.com/post");
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(await page.evaluate(() => window.innerWidth));
});

test("an email opens to its full text and folds back", async ({ page }) => {
  const email = page.getByRole("region", { name: "邮件 · 收到一封" });
  await expect(email.getByText("流程表附在后面")).toHaveCount(0);
  await email.getByRole("button", { name: "Show full email" }).click();
  await expect(email.getByText(/流程表附在后面/)).toBeVisible();
  await email.getByRole("button", { name: "Collapse" }).click();
  await expect(email.getByText(/流程表附在后面/)).toHaveCount(0);
});

test("a long post folds and a short one has nothing to open", async ({ page }) => {
  const long = page.getByRole("region", { name: "帖子 · 长草稿" });
  const last = long.getByText(/第 16 段/);
  const folded = await long.locator(".response-card__body").boundingBox();
  await long.getByRole("button", { name: "Show" }).click();
  const opened = await long.locator(".response-card__body").boundingBox();
  expect(folded && opened && opened.height > folded.height).toBe(true);
  await expect(last).toBeVisible();
  await expect(page.getByRole("region", { name: "帖子 · 各平台" }).getByRole("button", { name: "Show" })).toHaveCount(0);
});

test("a post names its platform with its logo, and only a known platform has one", async ({ page }) => {
  const others = page.getByRole("region", { name: "帖子 · 其他平台" });
  await expect(others.locator('[data-integration-brand="youtube"]')).toHaveCount(1);
  await expect(others.locator('[data-integration-brand="linkedin"]')).toHaveCount(1);
  await expect(others.getByRole("article", { name: "未知平台" }).locator("[data-integration-brand]")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "帖子 · 多图图文" }).locator('[data-integration-brand="xiaohongshu"]')).toHaveCount(1);
});

test("an image post keeps every image in one swipeable row", async ({ page }) => {
  const gallery = page.getByRole("region", { name: "帖子 · 多图图文" }).getByRole("group");
  await expect(gallery.locator("> *")).toHaveCount(6);
  const box = await gallery.evaluate((element) => ({ scroll: element.scrollWidth, client: element.clientWidth }));
  expect(box.scroll).toBeGreaterThan(box.client);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(await page.evaluate(() => window.innerWidth));
});

test("a comment shows who wrote it, and a reply shows whether it went out", async ({ page }) => {
  const received = page.getByRole("region", { name: "评论 · 收到的" });
  await expect(received.getByText("@lumi.daily")).toBeVisible();
  await expect(received.getByRole("link", { name: "30 秒看完这台磨豆机值不值得买" })).toHaveAttribute("href", "https://example.com/post");
  await expect(received.getByText(/86/)).toBeVisible();
  const replies = page.getByRole("region", { name: "评论 · 回复草稿和已回复" }).locator(".response-card__reply");
  await expect(replies.nth(0)).toContainText("Draft");
  await expect(replies.nth(1)).toContainText("Sent");
  await expect(page.getByRole("region", { name: "评论 · 我们发出的" })).toContainText("Didn't go through");
});

test("a map pins the places it can locate and still lists the one it cannot", async ({ page }) => {
  const card = page.getByRole("region", { name: "地图" });
  await expect(card.locator(".response-card__map")).toHaveCount(0);
  await expect(card.getByText(/receives your IP address/)).toBeVisible();
  await Promise.all([
    page.waitForRequest("https://tile.openstreetmap.org/**"),
    card.getByRole("button", { name: "Load OpenStreetMap" }).click(),
  ]);
  await expect(card.locator(".response-card__map .response-card__pin")).toHaveCount(3);
  await expect(card.getByRole("link", { name: "© OpenStreetMap contributors" })).toBeVisible();
  await expect(card.getByRole("link", { name: "还没查到位置的场地" })).toHaveAttribute("href", /google\.com\/maps\/search/);
});

test("a person, an event, a quote and a product draw without a frame", async ({ page }) => {
  await expect(page.getByRole("region", { name: "名片" })).toContainText("市场经理");
  const events = page.getByRole("region", { name: "日程" });
  await expect(events).toContainText("Proposed");
  await expect(events).toContainText("Cancelled");
  const stock = page.getByRole("region", { name: "股票" });
  await expect(stock).toContainText("$128.4");
  await expect(stock.getByRole("img")).toBeVisible();
  await expect(page.getByRole("region", { name: "商品" }).getByRole("link", { name: "M2 平刀磨豆机" })).toHaveAttribute("href", "https://example.com/m2");
});

test("a chart names its values, shows them on hover and as a table", async ({ page }) => {
  const chart = page.getByRole("region", { name: "图表 · 趋势" });
  await expect(chart.getByRole("img", { name: /41 周 3\.1%/ })).toBeVisible();
  await chart.locator("svg rect").nth(1).hover();
  await expect(chart.locator(".response-card__tooltip")).toContainText("3.4%");
  await chart.getByRole("button", { name: "View as table" }).click();
  await expect(chart.getByRole("row", { name: /44 周/ })).toContainText("4.1%");
  await expect(chart.locator("svg")).toHaveCount(0);
});

test("a card this client cannot read shows its text instead of disappearing", async ({ page }) => {
  await expect(page.getByText("Broken card as text")).toBeVisible();
});

test("a card follows the sentence that introduces it, whenever its tool ran", async ({ page }) => {
  const reply = page.locator("#ordered");
  const sentence = await reply.getByText("It went out to everyone.").boundingBox();
  const card = await reply.getByRole("region", { name: "Ordered card" }).boundingBox();
  expect(sentence && card && sentence.y < card.y).toBe(true);
});
