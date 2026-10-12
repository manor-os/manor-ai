import { expect, test } from "@playwright/test";

for (const width of [1440, 390]) {
  test(`independent Agent commands save and delete at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
    const config: any = { version: 1, eyebrow: null, title: null, description: null, hero: null,
      entries: {}, prompts: {}, targets: {}, labels: {}, descriptions: {}, forms: {}, order: [], hidden: [],
      visual: "plain", layout: "feature", motion_cover_url: null, motion_cover_source_url: null };
    const writes: any[] = [];
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/v1/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith("/app-view")) {
        const actions = Object.entries(config.entries).map(([key, entry]: [string, any]) => ({
          key, kind: "chat", source_title: entry.title, title: config.labels[key] || entry.title,
          description: "", prompt: config.prompts[key], available: true,
        }));
        return route.fulfill({ json: { workspace_id: "app-fixture", title: "Empty App", description: "", status: "active",
          can_start: true, hero: null, config, actions, available_actions: actions,
          agent_options: [{ id: "writer", name: "Writer" }], workflow_options: [], automation_options: [], attachable_workflow_options: [],
        } });
      }
      if (path.endsWith("/operation/drafts")) {
        writes.push(route.request().postDataJSON());
        return route.fulfill({ json: { id: "draft", validation: { valid: true } } });
      }
      if (path.endsWith("/draft/apply")) {
        const update = writes.at(-1).patches[0].payload;
        for (const field of ["entries", "prompts", "targets", "labels", "descriptions", "forms"]) {
          for (const [key, value] of Object.entries(update[field])) {
            if (value === null) delete config[field][key]; else config[field][key] = value;
          }
        }
        return route.fulfill({ json: { status: "applied" } });
      }
      return route.fulfill({ json: [] });
    });
    await page.goto("/e2e/fixtures/workspace-app-config.html");
    for (const [name, prompt] of [["Draft", "Write a concise draft"], ["Review", "Review the draft"]]) {
      await page.getByRole("button", { name: "Add action", exact: true }).click();
      await page.getByRole("textbox", { name: "Display name for New action", exact: true }).fill(name);
      await page.getByRole("textbox", { name: `Agent command ${name}`, exact: true }).fill(prompt);
      await page.getByRole("button", { name: `Agent ${name}`, exact: true }).click();
      await page.getByRole("option", { name: "Writer", exact: true }).click();
    }
    await page.screenshot({ path: testInfo.outputPath(`direct-entries-${width}.png`), fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect.poll(() => writes.length).toBe(1);
    await expect(page.getByRole("button", { name: "Save", exact: true })).toBeDisabled();
    const [draftKey, reviewKey] = Object.keys(writes[0].patches[0].payload.entries);
    expect(writes[0].patches[0].payload.targets).toEqual({ [draftKey]: "agent:writer", [reviewKey]: "agent:writer" });
    await page.reload();
    await expect(page.getByRole("textbox", { name: "Agent command Draft", exact: true })).toHaveValue("Write a concise draft");
    await page.getByRole("button", { name: "Remove action Draft", exact: true }).click();
    await expect(page.getByRole("group", { name: "Draft", exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect.poll(() => writes.length).toBe(2);
    expect(writes[1].patches[0].payload.entries[draftKey]).toBeNull();
    await expect(page.getByRole("button", { name: "Save", exact: true })).toBeDisabled();
    await page.reload();
    await expect(page.getByRole("group", { name: "Draft", exact: true })).toHaveCount(0);
    await expect(page.getByRole("group", { name: "Review", exact: true })).toBeVisible();
    expect(errors).toEqual([]);
  });
}
