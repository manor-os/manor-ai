import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.route("**/config", (route) => route.fulfill({ json: {} }));
});

for (const width of [1440, 390]) {
  test(`all App entries use the same Agent, Flow and Automation selection at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
    const errors: string[] = [];
    const writes: any[] = [];
    const applies: unknown[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const config = { version: 1, eyebrow: null, title: null, description: null, hero: "service:write",
      order: [], hidden: [], labels: {}, descriptions: {}, forms: {}, targets: {}, visual: "plain", layout: "feature",
      motion_cover_url: null, motion_cover_source_url: null };
    const action = { key: "service:write", kind: "chat", title: "Write Article", source_title: "Write Article",
      source_description: "Ask the writer", description: "Ask the writer", default_agent_id: "agent", default_agent_name: "Writer" };
    await page.route("**/api/v1/**", async (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      if (path.endsWith("/app-view")) return route.fulfill({ json: {
        workspace_id: "app-fixture", title: "Studio", description: "Create articles", status: "active", can_start: true,
        hero: config.hero, config, actions: [action], available_actions: [action,
          { key: "automation:review", kind: "automation", title: "Review Progress", source_title: "Review Progress", description: "Review articles", job_id: "review" },
        ],
        workflow_options: [{ binding_id: "binding", workflow_id: "workflow", title: "Article Flow", description: "Generate article", inputs: [{ key: "topic", label: "Topic", required: true }] }],
        agent_options: [{ id: "agent", name: "Writer" }], automation_options: [{ job_id: "review", title: "Review Progress", enabled: true }],
        attachable_workflow_options: [{ workflow_id: "saved", title: "Saved Flow", inputs: [{ key: "brief" }] }],
      } });
      if (path.endsWith("/operation/drafts") && request.method() === "POST") {
        writes.push(request.postDataJSON());
        return route.fulfill({ json: { id: "draft", validation: { valid: true } } });
      }
      if (path.endsWith("/draft/apply")) {
        applies.push(request.postDataJSON());
        return route.fulfill({ json: { status: "applied" } });
      }
      return route.fulfill({ json: [] });
    });
    await page.goto("/e2e/fixtures/workspace-app-config.html");
    const mainAction = page.getByRole("combobox", { name: "Main action", exact: true });
    await mainAction.click();
    await expect(page.getByRole("listbox", { name: "Main action", exact: true }).getByRole("option")).toHaveText([
      "Article Flow", "Connect flow: Saved Flow",
    ]);
    await mainAction.press("Escape");
    await expect(mainAction).toBeFocused();
    const entry = page.getByRole("group", { name: "Write Article", exact: true });
    const runWith = entry.getByRole("button", { name: "Run with Write Article", exact: true });
    await expect(runWith).toHaveText("Agent");
    await runWith.focus();
    await expect(runWith).toBeFocused();
    await runWith.click();
    await expect(page.getByRole("listbox", { name: "Run with Write Article", exact: true }).getByRole("option")).toHaveText(["Agent", "Flow", "Automation"]);
    await runWith.press("ArrowDown");
    await runWith.press("Enter");
    await expect(runWith).toHaveText("Flow");
    await expect(entry.getByRole("button", { name: "Flow Write Article", exact: true })).toHaveText("Article Flow");
    await expect(page.getByRole("textbox", { name: "Description for Write Article", exact: true })).toHaveValue("Generate article");
    await expect(entry.getByText("The flow collects the inputs defined on its start node.")).toBeVisible();
    await expect(entry.getByText("What to collect first")).not.toBeVisible();
    await expect(entry.getByRole("link", { name: "Configure flow", exact: true })).toHaveAttribute("href", "/flows/workflow");
    await expect(page.getByRole("button", { name: "Run with Article Flow", exact: true })).toHaveText("Flow");
    await expect(page.getByRole("button", { name: "Run with Review Progress", exact: true })).toHaveText("Automation");
    await expect(page.getByRole("button", { name: "Run with Article Flow", exact: true })).toBeEnabled();
    await expect(page.getByRole("button", { name: "Run with Review Progress", exact: true })).toBeEnabled();
    await expect(page.getByRole("link", { name: "Configure automation", exact: true })).toBeVisible();
    for (const title of ["Article Flow", "Review Progress"]) {
      const row = page.getByRole("group", { name: title, exact: true });
      const type = row.getByRole("button", { name: `Run with ${title}`, exact: true });
      for (const kind of ["Agent", "Flow", "Automation"]) {
        await type.click();
        await expect(page.getByRole("listbox", { name: `Run with ${title}`, exact: true }).getByRole("option")).toHaveText(["Agent", "Flow", "Automation"]);
        await page.getByRole("option", { name: kind, exact: true }).click();
        await expect(type).toHaveText(kind);
        await expect(row.getByRole("button", { name: `${kind} ${title}`, exact: true })).toBeVisible();
        await expect(row.getByText("What to collect first")).toBeVisible({ visible: kind === "Agent" });
      }
    }
    const flowType = page.getByRole("button", { name: "Run with Article Flow", exact: true });
    await flowType.click();
    await page.getByRole("option", { name: "Flow", exact: true }).click();
    const automation = page.getByRole("group", { name: "Review Progress", exact: true });
    await automation.getByRole("button", { name: "Run with Review Progress", exact: true }).click();
    await page.getByRole("option", { name: "Agent", exact: true }).click();
    await automation.getByRole("button", { name: "Agent Review Progress", exact: true }).click();
    await page.getByRole("option", { name: "Writer", exact: true }).click();
    await expect(automation.getByRole("button", { name: "Agent Review Progress", exact: true })).toHaveText("Writer");
    await automation.getByRole("button", { name: "+ Add question", exact: true }).click();
    await expect(automation.getByRole("textbox", { name: "Question label 1", exact: true })).toBeVisible();
    await automation.getByRole("button", { name: "Run with Review Progress", exact: true }).click();
    await page.getByRole("option", { name: "Flow", exact: true }).click();
    await automation.getByRole("button", { name: "Flow Review Progress", exact: true }).click();
    await page.getByRole("option", { name: "Saved Flow, Connect to this Workspace on save", exact: true }).click();
    await expect(automation.getByRole("status")).toHaveText("Connect to this Workspace on save");
    await page.screenshot({ path: testInfo.outputPath(`app-config-${width}.png`), fullPage: true, animations: "disabled" });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    await runWith.click();
    await page.getByRole("option", { name: "Automation", exact: true }).click();
    await expect(entry.getByRole("button", { name: "Automation Write Article", exact: true })).toHaveText("Review Progress");
    await runWith.click();
    await page.getByRole("option", { name: "Agent", exact: true }).click();
    await expect(page.getByRole("textbox", { name: "Description for Write Article", exact: true })).toHaveValue("Ask the writer");
    await expect(entry.getByText("What to collect first")).toBeVisible();
    await runWith.click();
    await page.getByRole("option", { name: "Flow", exact: true }).click();
    await entry.getByRole("button", { name: "Flow Write Article", exact: true }).click();
    const menu = page.getByRole("listbox", { name: "Flow Write Article", exact: true });
    await expect(menu.getByRole("option")).toHaveText(["Article FlowIn this Workspace", "Saved FlowConnect to this Workspace on save"]);
    expect((await menu.boundingBox())!.height).toBeLessThanOrEqual(240);
    await menu.getByRole("textbox").fill("Saved");
    await expect(menu.getByRole("option")).toHaveCount(1);
    await page.getByRole("option", { name: "Saved Flow, Connect to this Workspace on save", exact: true }).click();
    await expect(entry.getByRole("button", { name: "Flow Write Article", exact: true })).toHaveText("Saved Flow");
    await expect(entry.getByRole("status")).toHaveText("Connect to this Workspace on save");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect.poll(() => writes.length).toBe(1);
    expect(writes[0].patches).toContainEqual({ op: "workflow_binding.attach", payload: { workflow_id: "saved", service_key: "write" } });
    expect(writes[0].patches).toContainEqual({ op: "workflow_binding.attach", payload: { workflow_id: "saved", action_key: "automation:review" } });
    expect(writes[0].patches[0].payload.forms["automation:review"][0].key).toBe("field_1");
    expect(writes[0].patches[0].payload.targets["service:write"]).toBeNull();
    await expect.poll(() => applies).toEqual([{ user_confirmation: true }]);
    expect(errors).toEqual([]);
  });
}

for (const width of [1440, 390]) {
  test(`empty Workspace can choose a saved Workflow as its App main action at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
    const writes: any[] = [];
    const applies: unknown[] = [];
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/v1/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith("/app-view")) return route.fulfill({ json: {
        workspace_id: "app-fixture", title: "Empty Studio", description: "", status: "active", can_start: true,
        hero: null, actions: [], available_actions: [], workflow_options: [], agent_options: [], automation_options: [],
        config: { version: 1, eyebrow: null, title: null, description: null, hero: null, order: [], hidden: [],
          labels: {}, descriptions: {}, forms: {}, targets: {}, visual: "plain", layout: "feature",
          motion_cover_url: null, motion_cover_source_url: null },
        attachable_workflow_options: [{ workflow_id: "saved", title: "Saved Flow", description: "Generate an article", inputs: [{ key: "topic" }] }],
      } });
      if (path.endsWith("/operation/drafts")) {
        writes.push(route.request().postDataJSON());
        return route.fulfill({ json: { id: "draft", validation: { valid: true } } });
      }
      if (path.endsWith("/draft/apply")) {
        applies.push(route.request().postDataJSON());
        return route.fulfill({ json: { status: "applied" } });
      }
      return route.fulfill({ json: [] });
    });
    await page.goto("/e2e/fixtures/workspace-app-config.html");
    const main = page.getByRole("combobox", { name: "Main action", exact: true });
    await expect(main).toBeVisible();
    await main.focus();
    await expect(main).toBeFocused();
    await main.click();
    await page.getByRole("option", { name: "Connect flow: Saved Flow", exact: true }).click();
    await expect(page.getByRole("complementary", { name: "Live preview" }).getByRole("button", { name: "Saved Flow Generate an article", exact: true })).toBeVisible();
    await expect(page.getByText("Save to add the selected flow to this page.")).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath(`empty-app-config-${width}.png`), fullPage: true, animations: "disabled" });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect.poll(() => writes.length).toBe(1);
    expect(writes[0].patches).toContainEqual({ op: "workflow_binding.attach", payload: { workflow_id: "saved", primary: true } });
    expect(writes[0].patches[0].payload.hero).toBeNull();
    await expect.poll(() => applies).toEqual([{ user_confirmation: true }]);
    expect(errors).toEqual([]);
  });
}
