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

test("saving Page settings retains publication record bindings and the Main action", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  const writes: any[] = [];
  const config = {
    version: 1, eyebrow: null, title: null, description: null, hero: "workflow:publish", order: [], hidden: [], labels: {}, descriptions: {}, forms: {}, targets: {}, visual: "plain", layout: "feature",
    motion_cover_url: null, motion_cover_source_url: null,
    data_sources: {
      campaigns: { type: "workflow_projects", project_type: "content_campaign", fields: [["work", "title"]], label_fields: [["work", "title"]], launch_workflows: [] },
      publications: { type: "project_records", parent: "campaigns", record_path: ["publication"], record_schema: "publication-receipt/v1", fields: [] },
      results: { type: "publication_stats", parent: "publications", subject: "campaigns", metric_keys: ["views"], fields: [] },
      proposals: { type: "project_documents", parent: "campaigns", document_path: ["proposal_document_id"], document_role: "Proposal", fields: [] },
      reports: { type: "project_documents", parent: "campaigns", document_path: ["report_document_id"], document_role: "Report", fields: [] },
    },
    components: [{ type: "data_view", schema_version: 1, key: "publication_data", title: "Publication tracking", context: { kind: "project", source: "campaigns", label: "Campaign" }, visibility: { block: "published", when: "has_observations" }, blocks: [{
      key: "published", type: "records", source: "publications", metrics_source: "results", title: "Published content",
      record_display: { title: { source: "campaigns", field: ["work", "title"] }, fields: [{ label: "Work", source: "campaigns", field: ["work", "title"] }] },
    }, { key: "proposal", type: "documents", source: "proposals", title: "Proposal" },
    { key: "reports", type: "documents", source: "reports", title: "Reports" }], display: { layout: "stack", hidden_blocks: ["proposal"] } }],
  };
  const action = { key: "workflow:publish", kind: "workflow", binding_id: "publish", workflow_id: "publish", title: "Publish content", source_title: "Publish content", description: "Start a new publication", inputs: [] };
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/app-view")) return route.fulfill({ json: {
      workspace_id: "app-fixture", title: "Publication studio", description: "", status: "active", can_start: true, hero: config.hero, config,
      actions: [action], available_actions: [action], workflow_options: [action], agent_options: [], automation_options: [], attachable_workflow_options: [],
    } });
    if (path.endsWith("/operation/drafts")) {
      writes.push(route.request().postDataJSON());
      return route.fulfill({ json: { id: "draft", validation: { valid: true } } });
    }
    if (path.endsWith("/draft/apply")) return route.fulfill({ json: { status: "applied" } });
    return route.fulfill({ json: [] });
  });
  await page.goto("/e2e/fixtures/workspace-app-config.html");
  await expect(page.getByRole("combobox", { name: "Main action", exact: true })).toBeVisible();
  await page.getByRole("textbox", { name: "Title", exact: true }).fill("Release dashboard");
  const preview = page.getByRole("complementary", { name: "Live preview" });
  await expect(preview.locator(".workspace-app-view__hero")).toBeVisible();
  await expect(preview.getByRole("button", { name: "Publish content Start a new publication", exact: true })).toBeVisible();
  const section = page.getByRole("region", { name: "Data on this page", exact: true });
  await expect(section.getByText("publication_data", { exact: true })).toHaveCount(0);
  await expect(section.getByRole("textbox", { name: "Content title Proposal", exact: true })).toHaveCount(0);
  await section.getByText("Hidden content (1)", { exact: true }).click();
  await section.getByRole("button", { name: "Show Proposal", exact: true }).click();
  await expect(section.getByRole("textbox", { name: "Content title Proposal", exact: true })).toBeVisible();
  await section.getByRole("button", { name: "Hide Proposal", exact: true }).click();
  await section.getByRole("button", { name: "Move up Reports", exact: true }).click();
  await expect(section.locator(".workspace-app-data-settings__block-copy input").nth(0)).toHaveValue("Reports");
  await expect(section.locator(".workspace-app-data-settings__block-copy input").nth(1)).toHaveValue("Published content");
  await section.getByRole("radio", { name: "In tabs", exact: false }).check();
  await section.getByRole("textbox", { name: "In tabs 1", exact: true }).fill("Overview");
  await section.getByRole("combobox", { name: "Add tab", exact: true }).selectOption("reports");
  await section.getByRole("button", { name: "Add tab", exact: true }).click();
  await section.getByRole("textbox", { name: "In tabs 2", exact: true }).fill("Reports");
  const reportsTab = await section.getByRole("combobox", { name: "In tabs Reports", exact: true }).inputValue();
  await section.getByRole("combobox", { name: "In tabs Published content", exact: true }).selectOption(reportsTab);
  await section.getByRole("group", { name: "How to arrange it", exact: true }).getByRole("button", { name: "Move up Reports", exact: true }).click();
  await expect(section.getByRole("textbox", { name: "In tabs 1", exact: true })).toHaveValue("Reports");
  await section.getByRole("button", { name: "Remove tab Overview", exact: true }).click();
  await expect(section.getByRole("textbox", { name: "In tabs 2", exact: true })).toHaveCount(0);
  await section.getByRole("radio", { name: "One after another", exact: false }).check();
  await section.getByRole("combobox", { name: "Add section", exact: true }).selectOption("proposal");
  await section.getByRole("button", { name: "Add section", exact: true }).click();
  await section.getByRole("textbox", { name: "Sections 1", exact: true }).fill("Publications");
  await section.getByRole("textbox", { name: "Sections 2", exact: true }).fill("Plan");
  const planSection = await section.getByRole("combobox", { name: "Section Published content", exact: true }).locator("option").filter({ hasText: "Plan" }).getAttribute("value");
  await section.getByRole("combobox", { name: "Section Published content", exact: true }).selectOption(planSection!);
  const visibility = page.getByRole("combobox", { name: "When to show", exact: true });
  await expect(visibility).toHaveValue("has_observations:published");
  await visibility.selectOption("");
  await expect(visibility).toHaveValue("");
  await visibility.selectOption("has_records:published");
  await expect(visibility).toHaveValue("has_records:published");
  await visibility.selectOption("has_observations:published");
  await section.getByRole("checkbox", { name: "Show only metrics configured for each publication", exact: true }).check();
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect.poll(() => writes.length).toBe(1);
  const saved = writes[0].patches[0].payload.components[0];
  expect(saved.blocks.map((block: any) => block.key)).toEqual(["reports", "proposal", "published"]);
  expect(saved.display.hidden_blocks).toEqual(["proposal"]);
  expect(saved.display.layout).toBe("stack");
  expect(saved.display.groups.map((group: any) => ({ title: group.title, blocks: group.blocks }))).toEqual([
    { title: "Publications", blocks: ["reports"] },
    { title: "Plan", blocks: ["proposal", "published"] },
  ]);
  const published = saved.blocks.find((block: any) => block.key === "published");
  expect(published.metrics_source).toBe("results");
  expect(published.metrics_display).toBe("configured");
  expect(published.record_display).toEqual(config.components[0].blocks[0].record_display);
  expect(writes[0].patches[0].payload.data_sources).toEqual(config.data_sources);
  expect(writes[0].patches[0].payload.components[0].visibility).toEqual(config.components[0].visibility);
  expect(writes[0].patches[0].payload.hero).toBe("workflow:publish");
  expect(writes[0].patches[0].payload.layout).toBe("feature");
});

test("saving Page data settings retains generic stages, ordering, rollups, and progress fields", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  const writes: any[] = [];
  const config = {
    version: 1, eyebrow: null, title: null, description: null, hero: null, order: [], hidden: [], labels: {}, descriptions: {}, forms: {}, targets: {}, visual: "plain", layout: "feature", motion_cover_url: null, motion_cover_source_url: null,
    data_sources: { milestones: { type: "workflow_projects", project_type: "milestone", fields: [["due_at"], ["status"]], stages: ["scheduled"], order: { field: ["due_at"], type: "datetime", direction: "asc" }, launch_workflows: [] } },
    components: [{ type: "data_view", schema_version: 1, key: "plan", title: "Plan", context: { kind: "project", source: "milestones" }, blocks: [
      { key: "progress", type: "progress", source: "milestones", title: "Milestones", fields: [{ label: "Due", field: ["due_at"], format: "date" }] },
      { key: "rollup", type: "project_rollup", source: "milestones", title: "Plan summary", count_by_stage: true, first: { stages: ["scheduled"] } },
    ] }],
  };
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/app-view")) return route.fulfill({ json: { workspace_id: "app-fixture", title: "Plan", description: "", status: "active", can_start: true, hero: null, config,
      actions: [], available_actions: [], workflow_options: [], agent_options: [], automation_options: [], attachable_workflow_options: [] } });
    if (path.endsWith("/operation/drafts")) { writes.push(route.request().postDataJSON()); return route.fulfill({ json: { id: "draft", validation: { valid: true } } }); }
    if (path.endsWith("/draft/apply")) return route.fulfill({ json: { status: "applied" } });
    return route.fulfill({ json: [] });
  });
  await page.goto("/e2e/fixtures/workspace-app-config.html");
  const section = page.getByRole("region", { name: "Data on this page", exact: true });
  const progress = section.locator(".workspace-app-data-settings__block").nth(0);
  await progress.locator("details").click();
  await progress.getByRole("textbox", { name: "Stages to include Milestones", exact: true }).fill("scheduled, ready");
  await progress.getByRole("combobox", { name: "Direction Milestones", exact: true }).selectOption("desc");
  await progress.getByRole("textbox", { name: "Field label 1", exact: true }).fill("Due date");
  await progress.getByRole("textbox", { name: "Stage labels Milestones", exact: true }).fill("scheduled: Scheduled\nready: Ready to start");
  await progress.getByRole("textbox", { name: "Empty message Milestones", exact: true }).fill("No milestones are planned yet.");
  const rollup = section.locator(".workspace-app-data-settings__block").nth(1);
  await rollup.locator("details").click();
  await rollup.getByRole("textbox", { name: "Stages considered pending Plan summary", exact: true }).fill("ready, verification_required");
  await rollup.getByRole("textbox", { name: "Stages considered complete Plan summary", exact: true }).fill("published, delivered");
  await rollup.getByRole("textbox", { name: "Completion label Plan summary", exact: true }).fill("Published");
  await rollup.getByRole("checkbox", { name: "Count work by stage", exact: true }).uncheck();
  await expect(rollup.getByRole("textbox", { name: "Stages considered complete Plan summary", exact: true })).toBeDisabled();
  await rollup.getByRole("checkbox", { name: "Count work by stage", exact: true }).check();
  await rollup.getByRole("textbox", { name: "Stages considered complete Plan summary", exact: true }).fill("published, delivered");
  await rollup.getByRole("textbox", { name: "Completion label Plan summary", exact: true }).fill("Published");
  await rollup.getByRole("textbox", { name: "Stage labels Plan summary", exact: true }).fill("ready: Ready\npublished: Published");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect.poll(() => writes.length).toBe(1);
  const payload = writes[0].patches[0].payload;
  expect(payload.layout).toBe("feature");
  expect(payload.data_sources.milestones).toMatchObject({ stages: ["scheduled", "ready"], order: { field: ["due_at"], type: "datetime", direction: "desc" } });
  expect(payload.components[0].blocks).toEqual(expect.arrayContaining([
    expect.objectContaining({ key: "progress", fields: [{ label: "Due date", field: ["due_at"], format: "date" }], empty_message: "No milestones are planned yet.", stage_labels: { scheduled: "Scheduled", ready: "Ready to start" } }),
    expect.objectContaining({ key: "rollup", count_by_stage: true, first: { stages: ["ready", "verification_required"] }, completion: { stages: ["published", "delivered"], label: "Published" }, stage_labels: { ready: "Ready", published: "Published" } }),
  ]));
});

test("saving an inspection page retains its declared media source without publication metrics", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  const writes: any[] = [];
  const config = {
    version: 1, eyebrow: null, title: null, description: null, hero: null, order: [], hidden: [], labels: {}, descriptions: {}, forms: {}, targets: {}, visual: "plain", layout: "feature",
    motion_cover_url: null, motion_cover_source_url: null,
    data_sources: {
      deliveries: { type: "workflow_projects", project_type: "delivery", fields: [], label_fields: [["title"]], launch_workflows: [] },
      inspections: { type: "project_records", parent: "deliveries", record_path: ["inspection"], record_schema: "inspection/v1", fields: [] },
      photos: { type: "project_documents", parent: "deliveries", document_path: ["photo_document_id"], document_role: "Inspection photo", fields: [] },
    },
    components: [{ type: "data_view", schema_version: 1, key: "delivery_review", title: "Delivery review", context: { kind: "project", source: "deliveries", label: "Delivery" }, blocks: [{
      key: "inspections", type: "records", source: "inspections", title: "Inspection", record_display: {},
    }] }],
  };
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/app-view")) return route.fulfill({ json: {
      workspace_id: "app-fixture", title: "Delivery studio", description: "", status: "active", can_start: true, hero: null, config,
      actions: [], available_actions: [], workflow_options: [], agent_options: [], automation_options: [], attachable_workflow_options: [],
    } });
    if (path.endsWith("/operation/drafts")) {
      writes.push(route.request().postDataJSON());
      return route.fulfill({ json: { id: "draft", validation: { valid: true } } });
    }
    if (path.endsWith("/draft/apply")) return route.fulfill({ json: { status: "applied" } });
    return route.fulfill({ json: [] });
  });
  await page.goto("/e2e/fixtures/workspace-app-config.html");
  const data = page.getByRole("region", { name: "Data on this page", exact: true });
  const visibility = data.getByRole("combobox", { name: "When to show", exact: true });
  await expect(visibility.locator("option")).toHaveText(["Always", "After this record exists"]);
  await data.getByRole("combobox", { name: "Add media source", exact: true }).selectOption("photos");
  await expect(data.getByRole("textbox", { name: "Label Inspection Photo", exact: true })).toBeVisible();
  await data.getByRole("textbox", { name: "Label Inspection Photo", exact: true }).fill("Site image");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect.poll(() => writes.length).toBe(1);
  const record = writes[0].patches[0].payload.components[0].blocks[0];
  expect(record.record_display.media).toEqual([{ source: "photos", label: "Site image" }]);
  expect(writes[0].patches[0].payload.data_sources).toEqual(config.data_sources);
});

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
