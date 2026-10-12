import { expect, test, type Page } from "@playwright/test";

async function mockWorkspace(page: Page, failures: { mapping?: boolean; registration?: boolean; workerBinding?: boolean } = {}) {
  const services = ["Research", "Leads", "Content"].map((name) => ({ key: name.toLowerCase(), name }));
  const agents: Record<string, any>[] = [...services.map((service) => ({ id: `${service.key}-agent`, name: `${service.name} Agent` })), { id: "video-agent", name: "Video Agent" }]
    .map((agent) => ({ ...agent, entity_id: "fixture-entity", system_prompt: "Perform your assigned service.", config: {}, status: "active", tags: [] }));
  const mappings = services.map((service) => ({ id: `sub-${service.key}`, service_key: service.key, agent_id: `${service.key}-agent` }));
  const writes: Record<string, any>[] = [];
  const errors: string[] = [];
  const bound = new Set<string>();
  let failSkill = true;
  let skillWrites = 0;
  const createdAgents: Record<string, any>[] = [];
  const updatedAgents: Record<string, any>[] = [];
  const registrations: Record<string, any>[] = [];
  const workerBindings: Record<string, any>[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  await page.route("**/config", (route) => route.fulfill({ json: { deployment_mode: "oss", flows_available: false, ai_credits_unlimited: true } }));
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/workspaces/agent-fixture/agents" && request.method() === "POST") {
      const body = request.postDataJSON();
      writes.push(body);
      if (failures.mapping) {
        failures.mapping = false;
        return route.fulfill({ status: 400, json: { detail: "Synthetic mapping failure" } });
      }
      if (body.new_service && services.some((service) => service.key === body.service_key)) {
        return route.fulfill({ status: 409, json: { detail: "This service already exists" } });
      }
      if (body.new_service) services.push(body.new_service);
      const existing = mappings.find((mapping) => mapping.service_key === body.service_key);
      if (existing) existing.agent_id = body.agent_id;
      else mappings.push({ id: `sub-${body.service_key}`, service_key: body.service_key, agent_id: body.agent_id });
      return route.fulfill({ json: { status: "applied" } });
    }
    if (path === "/api/v1/workspaces/agent-fixture") return route.fulfill({ json: { id: "agent-fixture", name: "Agent configuration fixture", status: "paused", settings: {}, services } });
    if (path.endsWith("/operating-model")) return route.fulfill({ json: { services, agent_mappings: mappings } });
    if (path.endsWith("/connection-status")) return route.fulfill({ json: { workspace_id: "agent-fixture", requirements: [], required_issue_count: 0 } });
    if (path === "/api/v1/workspaces/agent-fixture/agents") return route.fulfill({ json: mappings });
    if (path === "/api/v1/agents") {
      if (request.method() === "POST") {
        const agent = { id: `created-agent-${createdAgents.length + 1}`, entity_id: "fixture-entity", status: "active", ...request.postDataJSON() };
        createdAgents.push(agent);
        agents.push(agent);
        return route.fulfill({ json: agent });
      }
      return route.fulfill({ json: agents });
    }
    if (path === "/api/v1/workers/register") {
      registrations.push(request.postDataJSON());
      if (failures.registration) {
        failures.registration = false;
        return route.fulfill({ status: 400, json: { detail: "Synthetic registration failure" } });
      }
      return route.fulfill({ json: { worker_id: "fixture-worker", worker_secret: "synthetic-connection-secret" } });
    }
    if (path.startsWith("/api/v1/agents/subscriptions/") && path.endsWith("/workers") && request.method() === "POST") {
      workerBindings.push(request.postDataJSON());
      if (failures.workerBinding) {
        failures.workerBinding = false;
        return route.fulfill({ status: 400, json: { detail: "Synthetic worker binding failure" } });
      }
      return route.fulfill({ json: { id: "fixture-worker-binding" } });
    }
    if (path === "/api/v1/skills") return route.fulfill({ json: [{ id: "video-skill", name: "Video production", description: "Create product videos" }] });
    if (path.endsWith("/bindings/video-skill")) {
      skillWrites += 1;
      if (failSkill) return route.fulfill({ status: 503, json: { detail: "Synthetic skill binding failure" } });
      bound.add("video-skill");
      return route.fulfill({ json: { id: "video-skill" } });
    }
    if (path.endsWith("/bindings")) return route.fulfill({ json: [...bound].map((id) => ({ id })) });
    const agent = agents.find((entry) => path === `/api/v1/agents/${entry.id}`);
    if (agent) {
      if (request.method() === "PUT") {
        updatedAgents.push(request.postDataJSON());
        Object.assign(agent, request.postDataJSON());
      }
      return route.fulfill({ json: agent });
    }
    return route.fulfill({ json: [] });
  });
  return { writes, mappings, errors, createdAgents, updatedAgents, registrations, workerBindings, allowSkill: () => { failSkill = false; }, skillWrites: () => skillWrites };
}

async function openHttpsAgent(page: Page) {
  await page.goto("/e2e/fixtures/workspace-agent-config.html");
  await page.getByRole("button", { name: "Add agent", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Add agent to workspace" });
  await dialog.getByLabel("New service name").fill("Marketing Video");
  await dialog.getByRole("button", { name: /HTTPS agent/i }).click();
  await dialog.getByLabel("Agent name", { exact: true }).fill("Initial Video Agent");
  await dialog.getByPlaceholder("Leave blank to generate a focused workspace prompt.", { exact: true }).fill("Initial video instructions.");
  await dialog.getByLabel("HTTPS endpoint", { exact: true }).fill("https://initial.example.test/agent");
  return dialog;
}

test("mapping retry saves edited fields on the already created Agent", async ({ page }) => {
  const state = await mockWorkspace(page, { mapping: true });
  const dialog = await openHttpsAgent(page);
  await dialog.getByRole("button", { name: "Add agent", exact: true }).click();
  await expect(page.getByText("Synthetic mapping failure", { exact: true }).first()).toBeVisible();
  await dialog.getByLabel("Agent name", { exact: true }).fill("Revised Video Agent");
  await dialog.getByPlaceholder("Leave blank to generate a focused workspace prompt.", { exact: true }).fill("Revised video instructions.");
  await dialog.getByLabel("HTTPS endpoint", { exact: true }).fill("https://revised.example.test/agent");
  await dialog.getByRole("button", { name: "Add agent", exact: true }).click();
  await expect(dialog.getByText("synthetic-connection-secret", { exact: true })).toBeVisible();
  expect(state.createdAgents).toHaveLength(1);
  expect(state.updatedAgents).toHaveLength(1);
  expect(state.createdAgents[0]).toMatchObject({ name: "Revised Video Agent", system_prompt: "Revised video instructions.", config: { runtime_connection: { endpoint_url: "https://revised.example.test/agent" } } });
  expect(state.mappings.filter((mapping) => mapping.service_key === "marketing_video")).toHaveLength(1);
  expect(state.errors).toEqual([]);
});

for (const failure of ["registration", "workerBinding"] as const) {
  test(`HTTPS ${failure} retry retains the created Agent, service, and completed worker registration`, async ({ page }) => {
    const state = await mockWorkspace(page, { [failure]: true });
    const dialog = await openHttpsAgent(page);
    await dialog.getByRole("button", { name: "Add agent", exact: true }).click();
    await expect(page.getByText(failure === "registration" ? "Synthetic registration failure" : "Synthetic worker binding failure", { exact: true }).first()).toBeVisible();
    await dialog.getByRole("button", { name: /^(Add agent|Replace service agent)$/ }).click();
    await expect(dialog.getByText("synthetic-connection-secret", { exact: true })).toBeVisible();
    expect(state.createdAgents).toHaveLength(1);
    expect(state.writes.filter((write) => write.new_service)).toHaveLength(1);
    expect(state.mappings.filter((mapping) => mapping.service_key === "marketing_video")).toHaveLength(1);
    expect(state.registrations).toHaveLength(failure === "registration" ? 2 : 1);
    expect(state.workerBindings).toHaveLength(failure === "workerBinding" ? 2 : 1);
    expect(state.errors).toEqual([]);
  });
}

for (const width of [1440, 390]) {
  test(`add distinct agent and make replacement explicit at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    const state = await mockWorkspace(page);
    await page.goto("/e2e/fixtures/workspace-agent-config.html");
    await page.getByRole("button", { name: "Add agent", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Add agent to workspace" });
    await expect(dialog.getByRole("button", { name: "Add agent", exact: true })).toBeDisabled();
    await dialog.getByLabel("New service name").fill("Marketing Video");
    await dialog.getByRole("button", { name: "Manor hosted agent", exact: true }).click();
    await page.getByRole("option", { name: "Video Agent", exact: true }).click();
    await expect(dialog.getByRole("button", { name: "Manor hosted agent", exact: true })).toHaveAttribute("aria-expanded", "false");
    await expect(dialog.getByRole("button", { name: "Add agent", exact: true })).toBeEnabled();
    await dialog.getByRole("button", { name: "Add agent", exact: true }).focus();
    await expect(dialog.getByRole("button", { name: "Add agent", exact: true })).toBeFocused();
    await dialog.screenshot({ path: testInfo.outputPath(`add-agent-${width}.png`) });
    expect(await dialog.evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
    await dialog.getByRole("button", { name: "Add agent", exact: true }).press("Enter");
    await expect(dialog).not.toBeVisible();
    await expect(page.getByRole("cell", { name: "Marketing Video", exact: true })).toBeVisible();
    expect(state.writes[0]).toEqual({ service_key: "marketing_video", agent_id: "video-agent", new_service: { key: "marketing_video", name: "Marketing Video" } });
    expect(state.mappings).toHaveLength(4);
    expect(state.mappings.find((mapping) => mapping.service_key === "content")?.agent_id).toBe("content-agent");
    await page.getByRole("button", { name: "Add agent", exact: true }).click();
    await dialog.getByLabel("New service name").fill("Marketing Video");
    await expect(dialog.getByRole("alert")).toContainText("already exists");
    await expect(dialog.getByRole("button", { name: "Add agent", exact: true })).toBeDisabled();
    await dialog.getByRole("button", { name: "Service assignment", exact: true }).click();
    await page.getByRole("option", { name: "Existing service", exact: true }).click();
    await dialog.getByRole("button", { name: "Existing service", exact: true }).click();
    await page.getByRole("option", { name: "Content · Replace service agent", exact: true }).click();
    await expect(dialog.getByRole("status")).toContainText("already has an agent");
    await expect(dialog.getByRole("button", { name: "Replace service agent", exact: true })).toBeVisible();
    expect(state.errors).toEqual([]);
  });
}

test("failed skill update stays open and retries before reporting success", async ({ page }) => {
  const state = await mockWorkspace(page);
  await page.goto("/e2e/fixtures/workspace-agent-config.html");
  await page.getByRole("button", { name: "Edit agent", exact: true }).first().click();
  const dialog = page.getByRole("dialog", { name: "Edit Agent", exact: true });
  await dialog.getByRole("button", { name: /Video production/i }).click();
  await dialog.getByRole("button", { name: "Update Agent", exact: true }).click();
  await expect(page.getByText("Agent details saved, but some tool or skill changes failed. Review your selections and save again.")).toBeVisible();
  await expect(dialog).toBeVisible();
  expect(state.skillWrites()).toBe(1);
  state.allowSkill();
  await dialog.getByRole("button", { name: "Update Agent", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  expect(state.skillWrites()).toBe(2);
  await page.getByRole("button", { name: "Edit agent", exact: true }).first().click();
  await expect(dialog.getByRole("button", { name: /Video production/i }).getByRole("checkbox")).toBeChecked();
  expect(state.errors).toEqual([]);
});
