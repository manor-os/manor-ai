import { expect, request as pwRequest, test } from "@playwright/test";

// Live user turn -> model tool calls -> SSE assistant blocks -> persisted
// history coverage. This intentionally requires a working local/BYOK model.
// E2E_DOCKER_WORKSPACE_LEDGER=1 E2E_API=http://127.0.0.1:8010 \
// npm run test:e2e -- workspace-chat-ledgers.spec.ts

const API = process.env.E2E_API ?? "http://localhost:8000";
const RUN_DOCKER_E2E = process.env.E2E_DOCKER_WORKSPACE_LEDGER === "1";

test.skip(!RUN_DOCKER_E2E, "Set E2E_DOCKER_WORKSPACE_LEDGER=1 for the live-stack test");

test("Workspace Chat renders live Ledger tool results before history hydration", async ({ page }) => {
  const api = await pwRequest.newContext({ baseURL: API });
  const suffix = Date.now();
  let token = "";
  let workspaceId = "";

  try {
    const login = await api.post("/api/v1/auth/login", {
      data: { email: "demo@manor.local", password: "manor-demo" },
    });
    expect(login.ok(), `login failed: ${login.status()}`).toBeTruthy();
    const loginPayload = await login.json();
    token = loginPayload.access_token;
    const headers = { Authorization: `Bearer ${token}` };

    const workspaceResponse = await api.post("/api/v1/workspaces", {
      headers,
      data: {
        name: `Recruiting Operations ${suffix}`,
        operating_context: "Recruit candidates, manage onboarding, and track finance.",
        primary_work: "Run interviews and operate the hiring pipeline.",
        heartbeat_enabled: false,
      },
    });
    expect(workspaceResponse.ok(), `workspace failed: ${workspaceResponse.status()}`).toBeTruthy();
    workspaceId = (await workspaceResponse.json()).id;

    let holdHistoryHydration = false;
    let releaseHistoryHydration = () => {};
    const historyHydrationGate = new Promise<void>((resolve) => {
      releaseHistoryHydration = resolve;
    });
    await page.route(
      `**/api/v1/workspaces/${workspaceId}/chat/messages/page?**`,
      async (route) => {
        if (holdHistoryHydration) await historyHydrationGate;
        await route.continue();
      },
    );
    await page.addInitScript((accessToken) => {
      window.localStorage.setItem("manor_token", accessToken as string);
      window.localStorage.setItem("manor_locale", "en");
      window.localStorage.setItem("manor_tour_completed", "true");
      window.localStorage.setItem("manor_consent_v1", JSON.stringify({
        v: 1,
        ts: new Date().toISOString(),
        locale: "en",
        categories: { functional: false, analytics: false, marketing: false },
      }));
    }, token);

    await page.setViewportSize({ width: 1180, height: 820 });
    await page.goto(`/chat?workspace=${workspaceId}`);

    holdHistoryHydration = true;
    const prompt = [
      "Use record_recruiting_ledger with",
      `record_key=e2e-candidate-${suffix}, subject_type=candidate, display_name=E2E Candidate,`,
      `stage=screening, status=active, event=screening_started, idempotency_key=e2e-recruiting-${suffix}.`,
      "Then use record_recruiting_ledger with",
      `record_key=e2e-offer-${suffix}, subject_type=candidate, display_name=E2E Offer Candidate,`,
      `stage=offer, status=active, event=offer_sent, idempotency_key=e2e-recruiting-offer-${suffix}.`,
      "Then use record_finance_ledger with entry_type=income, status=posted,",
      `amount_minor=125000, currency=USD, direction=inflow, account_ref=sales, idempotency_key=e2e-finance-${suffix}.`,
      "Then call query_ledger for recruiting_ledger view=current group_by=[stage] metrics=[count].",
      "Finally call visualize_workspace_ledgers. Do not answer before all five tools succeed.",
    ].join(" ");
    const composer = page.locator(".chat-composer-rich-editor");
    await composer.fill(prompt);
    await page.locator("button.chat-composer-send").click();

    const visualization = page.locator(".assistant-ledger-visualization").first();
    await expect(visualization).toBeVisible({ timeout: 120_000 });
    await expect(page.locator(".assistant-ledger-visualization").nth(1)).toBeVisible();
    const rendered = page
      .frameLocator("iframe.assistant-ledger-visualization__frame")
      .nth(1)
      .frameLocator("#manor-generated-preview");
    await expect(rendered.getByRole("heading", { name: "Business Ledger overview" })).toBeVisible();
    await expect(rendered.getByText("Recruiting & HR", { exact: true })).toBeVisible();
    await expect(rendered.getByText("Screening", { exact: true })).toBeVisible();
    await expect(rendered.getByText("Finance", { exact: true })).toBeVisible();
    await expect(rendered.getByText("USD", { exact: true })).toBeVisible();
    const queryRendered = page
      .frameLocator("iframe.assistant-ledger-visualization__frame")
      .nth(0)
      .frameLocator("#manor-generated-preview");
    await expect(queryRendered.getByRole("heading", { name: "Recruiting & HR" })).toBeVisible();
    await expect(queryRendered.getByText("Screening", { exact: true })).toBeVisible();
    await expect(queryRendered.getByText("Offer", { exact: true })).toBeVisible();
    holdHistoryHydration = false;
    releaseHistoryHydration();

    await page.evaluate(() => {
      document.documentElement.dataset.theme = "dark";
    });
    await expect.poll(() => rendered.locator("body").evaluate(
      (body) => getComputedStyle(body).backgroundColor,
    )).toBe("rgb(18, 18, 17)");
    await page.evaluate(() => {
      document.documentElement.dataset.theme = "light";
    });

    await page.getByRole("button", { name: "Configure business Ledgers" }).click();
    const configurationDialog = page.getByRole("dialog", {
      name: "Configure business Ledgers",
    });
    await expect(configurationDialog).toBeVisible();
    const contentLedger = configurationDialog.getByRole("checkbox", { name: /Content/ });
    await expect(contentLedger).not.toBeChecked();
    await contentLedger.click();
    const configurationResponse = page.waitForResponse((response) => (
      response.request().method() === "PUT"
      && response.url().includes(`/workspaces/${workspaceId}/ledgers/configuration`)
    ));
    await configurationDialog.getByRole("button", { name: "Save" }).click();
    expect((await configurationResponse).ok()).toBeTruthy();
    await expect(configurationDialog).toBeHidden();

    if (process.env.E2E_LEDGER_SCREENSHOT) {
      await page.screenshot({
        path: process.env.E2E_LEDGER_SCREENSHOT,
        fullPage: true,
      });
    }

    await page.getByTestId("workspace-chat-stats-trigger").click();
    await expect(page.getByRole("tab", { name: "Ledgers", exact: true })).toHaveCount(0);
    await expect(page.getByRole("tab", { name: "Metrics", exact: true })).toBeVisible();
    await page.keyboard.press("Escape");

    await page.reload();
    await expect(page.locator(".assistant-ledger-visualization").first()).toBeVisible();
    await expect(
      page.frameLocator("iframe.assistant-ledger-visualization__frame").nth(0)
        .frameLocator("#manor-generated-preview")
        .getByText("Recruiting & HR", { exact: true }),
    ).toBeVisible();

    await page.setViewportSize({ width: 344, height: 568 });
    await expect.poll(async () => {
      const box = await page.locator(".assistant-ledger-visualization").first().boundingBox();
      return Boolean(box && box.x >= 0 && box.x + box.width <= 344);
    }).toBe(true);
  } finally {
    if (workspaceId && token) {
      await api.delete(`/api/v1/workspaces/${workspaceId}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
    }
    await api.dispose();
  }
});
