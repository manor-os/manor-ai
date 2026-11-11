import { expect, test } from "@playwright/test";

test("floating chat shows the question a run is parked on and sends the answer", async ({ page }) => {
  const answers: unknown[] = [];
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/v1/chat/runs/run_1/input") {
      answers.push(route.request().postDataJSON());
      return route.fulfill({ contentType: "application/json", body: JSON.stringify({ run_id: "run_1", status: "running" }) });
    }
    return route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
  });

  await page.goto("/e2e/fixtures/floating-runtime-input.html");
  await page.locator("button.float-chat-btn").click();

  const panel = page.locator("#floating-chat-panel");
  await expect(panel.getByText("Should the report include live clips?")).toBeVisible();
  await panel.getByRole("button", { name: /Leave them out/ }).click();
  await expect.poll(() => answers).toHaveLength(1);
  expect(answers[0]).toMatchObject({ request_id: "request_1" });
});
