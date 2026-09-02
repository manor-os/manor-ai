import { expect, test, type Page } from "@playwright/test";

const fixture = "/e2e/fixtures/webchat-page.html?api=1";
const saveUrl = "**/api/v1/workspaces/fixture/channels/binding";
const config = { version: 1, modules: [
  { id: "brand", side: "left", type: "brand", name: "Acme", website: "https://example.com", logo_url: "" },
  { id: "form", side: "right", type: "form", title: "Tour request", fields: ["Name", "Preferred time"] },
] };

async function publicChat(page: Page, embedded = false, publicPage: unknown = config) {
  await page.addInitScript(() => {
    localStorage.setItem("manor_public_chat_session:page-test", "visitor");
    localStorage.removeItem("manor_token");
  });
  await page.route("**/config", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/public/chat/page-test", route => route.fulfill({ json: {
    channel_name: "Webchat", agent_name: "Consultant", workspace_name: "Acme Workspace", language: "en", public_page: publicPage,
  } }));
  await page.route("**/api/v1/public/chat/page-test/session", route => route.fulfill({ json: { session_id: "visitor", conversation_id: "conversation", channel_config_id: "channel" } }));
  await page.route("**/api/v1/public/chat/page-test/messages?**", route => route.fulfill({ json: { messages: [], updates: [] } }));
  await page.goto(`/chat/public/page-test${embedded ? "?embed=1" : ""}`);
  await expect(page.locator("textarea")).toBeVisible();
}

test("native dragging moves a module across sides; review and save preserve it", async ({ page }) => {
  let saved: any;
  await page.route(saveUrl, route => { saved = route.request().postDataJSON(); return route.fulfill({ json: {} }); });
  await page.goto(fixture);
  await page.getByRole("tab", { name: "Edit", exact: true }).click();
  const source = page.getByRole("button", { name: "Move Text", exact: true });
  await source.dragTo(page.locator('[data-webchat-side="right"] .webchat-page-drop-end'));
  await expect(page.locator('[data-webchat-side="right"] [data-module-id="intro"]')).toBeVisible();
  await page.locator('[data-module-id="intro"]').getByRole("button", { name: "Configure", exact: true }).click();
  await page.getByRole("textbox", { name: "Title (optional)", exact: true }).fill("Updated introduction");
  await page.getByRole("tab", { name: "Review", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Updated introduction" })).toBeVisible();
  await page.getByRole("button", { name: "Save page", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(saved.config.public_page.modules.find((item: any) => item.id === "intro")).toMatchObject({ side: "right", title: "Updated introduction" });
  expect(Object.keys(saved.config)).toEqual(["public_page"]);
  await page.getByRole("button", { name: "Open page editor" }).click();
  await expect(page.getByRole("heading", { name: "Updated introduction" })).toBeVisible();
});

test("failed saves retain the draft; invalid URLs cannot be published; discard restores state", async ({ page }) => {
  await page.route(saveUrl, route => route.fulfill({ status: 403, json: { detail: "Workspace management required" } }));
  await page.goto(fixture);
  await page.getByRole("tab", { name: "Edit", exact: true }).click();
  await page.locator('[data-module-id="brand"]').getByRole("button", { name: "Configure", exact: true }).click();
  const website = page.getByRole("textbox", { name: "Website URL (optional)", exact: true });
  await website.fill("javascript:alert(1)");
  await page.getByRole("tab", { name: "Review", exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.getByRole("tab", { name: "Edit", exact: true })).toHaveAttribute("aria-selected", "true");
  await website.fill("https://new.example.com");
  await page.getByRole("tab", { name: "Review", exact: true }).click();
  await page.getByRole("button", { name: "Save page", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Workspace management required");
  await expect(page.getByRole("link", { name: "new.example.com", exact: false })).toBeVisible();
  await page.getByRole("button", { name: "Discard changes" }).click();
  await expect(page.getByRole("link", { name: "new.example.com", exact: false })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "example.com", exact: false })).toBeVisible();
});

test("read-only review offers no editor or save controls", async ({ page }) => {
  await page.goto(`${fixture}&readonly=1&stale=1`);
  await expect(page.getByRole("heading", { name: "Acme", exact: true })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Edit", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Save page", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Discard changes", exact: true })).toHaveCount(0);
});

test("an invalid stored page can be reviewed and cleared", async ({ page }) => {
  let saved: any;
  await page.route(saveUrl, route => { saved = route.request().postDataJSON(); return route.fulfill({ json: {} }); });
  await page.goto(`${fixture}&invalid-initial=1`);
  await expect(page.getByRole("alert")).toBeVisible();
  await page.getByRole("tab", { name: "Review", exact: true }).click();
  await page.getByRole("button", { name: "Save page", exact: true }).click();
  expect(saved.config.public_page).toEqual({ version: 1, modules: [] });
});

test("Review failure stays in Edit and cannot publish an unvalidated page", async ({ page }) => {
  await page.goto(`${fixture}&review-fails=1`);
  await page.locator('[data-module-id="intro"]').getByRole("button", { name: "Configure", exact: true }).click();
  await page.getByRole("textbox", { name: "Title (optional)", exact: true }).fill("Needs validation");
  await page.getByRole("tab", { name: "Review", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Review unavailable");
  await expect(page.getByRole("tab", { name: "Edit", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("button", { name: "Save page", exact: true })).toHaveCount(0);
});

test("authoritative Review removes unavailable actions from the saved page", async ({ page }) => {
  let saved: any;
  await page.route(saveUrl, route => { saved = route.request().postDataJSON(); return route.fulfill({ json: {} }); });
  await page.goto(`${fixture}&stale=1`);
  await expect(page.getByRole("heading", { name: "Unavailable action", exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "Review", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Unavailable action", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Save page", exact: true }).click();
  expect(saved.config.public_page.modules.some((module: any) => module.id === "stale")).toBe(false);
});

test("keyboard movement, add and remove work without dragging", async ({ page }) => {
  await page.goto(fixture);
  await page.getByRole("tab", { name: "Edit", exact: true }).click();
  await page.getByRole("button", { name: "Move Text", exact: true }).press("Alt+ArrowRight");
  await expect(page.locator('[data-webchat-side="right"] [data-module-id="intro"]')).toBeVisible();
  await expect(page.getByRole("button", { name: "Move Text", exact: true })).toBeFocused();
  await page.getByRole("button", { name: "Move Text", exact: true }).press("Alt+ArrowUp");
  await expect(page.locator('[data-webchat-side="right"] [data-module-id]').first()).toHaveAttribute("data-module-id", "intro");
  await page.locator('[data-webchat-side="left"]').getByRole("button", { name: "Add module", exact: true }).click();
  await page.getByRole("button", { name: "Image", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Public image URL", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Remove", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Public image URL", exact: true })).toHaveCount(0);
});

test("builder panels receive focus and restore it to their canvas trigger", async ({ page }) => {
  await page.goto(fixture);
  const intro = page.locator('[data-module-id="intro"]');
  await intro.getByRole("button", { name: "Configure", exact: true }).click();
  await expect(page.locator(".webchat-page-inspector")).toBeFocused();
  await page.getByRole("button", { name: "Done", exact: true }).click();
  await expect(intro.getByRole("button", { name: "Configure", exact: true })).toBeFocused();

  const left = page.locator('[data-webchat-side="left"]');
  await left.getByRole("button", { name: "Add module", exact: true }).click();
  await expect(page.locator(".webchat-page-library")).toBeFocused();
  await page.getByRole("button", { name: "Image", exact: true }).click();
  await expect(page.locator(".webchat-page-inspector")).toBeFocused();
  await page.getByRole("button", { name: "Remove", exact: true }).click();
  await expect(left.getByRole("button", { name: "Add module", exact: true })).toBeFocused();
});

test("Workspace content and action modules use the selected resource references", async ({ page }) => {
  let saved: any;
  await page.route(saveUrl, route => { saved = route.request().postDataJSON(); return route.fulfill({ json: {} }); });
  await page.goto(fixture);
  await page.getByRole("tab", { name: "Edit", exact: true }).click();
  await page.locator('[data-webchat-side="left"]').getByRole("button", { name: "Add module", exact: true }).click();
  await page.getByRole("button", { name: "Workspace content", exact: true }).click();
  await expect(page.getByText("Workspace profile", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Done", exact: true }).click();
  await page.locator('[data-webchat-side="right"]').getByRole("button", { name: "Add module", exact: true }).click();
  await page.getByRole("button", { name: "Workspace action", exact: true }).click();
  await page.getByRole("button", { name: "Add field", exact: true }).click();
  await page.getByRole("textbox", { name: "Field label", exact: true }).fill("Name");
  await page.screenshot({ path: test.info().outputPath("workspace-builder.png"), fullPage: true });
  await page.getByRole("tab", { name: "Review", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Request a tour", exact: true })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("workspace-connectors.png"), fullPage: true });
  await page.getByRole("button", { name: "Save page", exact: true }).click();
  const modules = saved.config.public_page.modules;
  expect(modules.find((item: any) => item.type === "workspace_content")).toMatchObject({ source: "profile", resource_id: "workspace" });
  expect(modules.find((item: any) => item.type === "workspace_action")).toMatchObject({ binding_id: "tour-flow", fields: ["Name"] });
});

test("a new Brand module starts from the configured Workspace identity and logo", async ({ page }) => {
  await page.goto(fixture);
  await page.getByRole("tab", { name: "Edit", exact: true }).click();
  await page.locator('[data-webchat-side="right"]').getByRole("button", { name: "Add module", exact: true }).click();
  await page.getByRole("button", { name: "Brand", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Company / Workspace name", exact: true })).toHaveValue("Acme Workspace");
  await expect(page.getByRole("textbox", { name: "Public logo URL (optional)", exact: true })).toHaveValue("https://example.com/logo.png");
});

test("public side forms preserve an existing draft and never send automatically", async ({ page }) => {
  let sends = 0;
  page.on("request", request => { if (request.url().includes("/message/stream")) sends += 1; });
  await publicChat(page);
  await expect(page.getByRole("heading", { name: "Acme", exact: true })).toBeVisible();
  expect(await page.locator(".webchat-page-layout").evaluate(element => Array.from(element.children).map(child => child.className))).toEqual([
    "webchat-page-side webchat-page-side--left",
    "webchat-page-center",
    "webchat-page-side webchat-page-side--right",
  ]);
  await page.locator("textarea").fill("Keep my draft");
  await page.getByRole("textbox", { name: "Name", exact: true }).fill("Alex");
  await page.getByRole("textbox", { name: "Preferred time", exact: true }).fill("Friday afternoon");
  await page.getByRole("button", { name: "Add to chat", exact: true }).click();
  await expect(page.locator("textarea")).toHaveValue("Keep my draft\n\nTour request\nName: Alex\nPreferred time: Friday afternoon");
  expect(sends).toBe(0);
});

test("a published Workspace action submits to its connected public endpoint", async ({ page }) => {
  let submission: any;
  await page.route("**/api/v1/public/chat/page-test/actions/tour", route => { submission = route.request().postDataJSON(); return route.fulfill({ status: 202, json: { accepted: true, queued: true } }); });
  await publicChat(page, false, { version: 1, modules: [{
    id: "tour", side: "right", type: "workspace_action", title: "Tour request", description: "Request a visit", binding_id: "tour-flow", fields: ["Name"], submit_label: "Request tour",
  }] });
  await page.getByRole("textbox", { name: "Name", exact: true }).fill("Alex");
  await page.getByRole("button", { name: "Request tour", exact: true }).click();
  await expect(page.getByText("Submitted to the Workspace.", { exact: true })).toBeVisible();
  expect(submission).toMatchObject({ session_id: "visitor", values: { Name: "Alex" } });
  expect(submission.submission_id).toMatch(/^[A-Za-z0-9_-]{16,80}$/);
});

test("Workspace action fields safely support names inherited from Object.prototype", async ({ page }) => {
  let submission: any;
  await page.route("**/api/v1/public/chat/page-test/actions/special", route => { submission = route.request().postDataJSON(); return route.fulfill({ status: 202, json: { accepted: true, queued: true } }); });
  await publicChat(page, false, { version: 1, modules: [{
    id: "special", side: "right", type: "workspace_action", title: "Special fields", description: "Safe labels", binding_id: "flow", fields: ["toString", "constructor", "__proto__"], submit_label: "Send fields",
  }] });
  for (const [field, value] of [["toString", "alpha"], ["constructor", "beta"], ["__proto__", "gamma"]] as const) {
    const input = page.getByRole("textbox", { name: field, exact: true });
    await expect(input).toHaveValue("");
    await input.fill(value);
  }
  await page.getByRole("button", { name: "Send fields", exact: true }).click();
  await expect(page.getByText("Submitted to the Workspace.", { exact: true })).toBeVisible();
  expect(submission.values).toEqual(Object.fromEntries([["toString", "alpha"], ["constructor", "beta"], ["__proto__", "gamma"]]));
});

test("a failed Workspace action retry keeps the same idempotency key", async ({ page }) => {
  const submissionIds: string[] = [];
  let attempts = 0;
  await page.route("**/api/v1/public/chat/page-test/actions/tour", route => {
    attempts += 1;
    submissionIds.push(route.request().postDataJSON().submission_id);
    return route.fulfill(attempts === 1
      ? { status: 503, json: { detail: "Workflow could not be queued. Please try again." } }
      : { status: 202, json: { accepted: true, queued: true, duplicate: true } });
  });
  await publicChat(page, false, { version: 1, modules: [{
    id: "tour", side: "right", type: "workspace_action", title: "Tour request", description: "Request a visit", binding_id: "tour-flow", fields: ["Name"], submit_label: "Request tour",
  }] });
  await page.getByRole("textbox", { name: "Name", exact: true }).fill("Alex");
  await page.getByRole("button", { name: "Request tour", exact: true }).click();
  await expect(page.getByText("Could not submit. Try again.", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Request tour", exact: true }).click();
  await expect(page.getByText("Submitted to the Workspace.", { exact: true })).toBeVisible();
  expect(submissionIds).toHaveLength(2);
  expect(submissionIds[1]).toBe(submissionIds[0]);
});

test("mobile shows chat first, with expandable side content and no overflow", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await publicChat(page);
  expect(await page.locator(".webchat-page-layout").evaluate(element => Array.from(element.children).map(child => child.className))).toEqual([
    "webchat-page-center",
    "webchat-page-side webchat-page-side--left",
    "webchat-page-side webchat-page-side--right",
  ]);
  await expect(page.getByRole("heading", { name: "Acme", exact: true })).not.toBeVisible();
  await page.locator('.webchat-page-side--left > summary').click();
  await expect(page.getByRole("heading", { name: "Acme", exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: test.info().outputPath("mobile.png"), fullPage: true });
});

test("mobile editor stacks the canvas and full-width inspector without overflow", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(fixture);
  await page.locator('[data-module-id="intro"]').getByRole("button", { name: "Configure", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Title (optional)", exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: test.info().outputPath("mobile-editor.png"), fullPage: true });
});

for (const scenario of ["embedded", "empty", "invalid"] as const) {
  test(`${scenario} configuration retains the original chat-only layout`, async ({ page }) => {
    await publicChat(page, scenario === "embedded", scenario === "empty" ? { version: 1, modules: [] } : scenario === "invalid" ? { version: 1, modules: [{ type: "html", html: "<script>" }] } : config);
    await expect(page.locator(".webchat-page-layout")).toHaveCount(0);
    await expect(page.locator("textarea")).toBeVisible();
  });
}
