import { expect, request as pwRequest, test } from "@playwright/test";

// Live-stack run:
// E2E_DOCKER_WORKSPACE_AUTONOMY=1 E2E_API=http://127.0.0.1:8010 \
// npm run test:e2e -- workspace-chat-goals.spec.ts

const API = process.env.E2E_API ?? "http://localhost:8000";
const RUN_DOCKER_E2E = process.env.E2E_DOCKER_WORKSPACE_AUTONOMY === "1";

test.skip(!RUN_DOCKER_E2E, "Set E2E_DOCKER_WORKSPACE_AUTONOMY=1 for the live-stack test");

test("lifecycle hover panel views and edits Goals while start automatically derives Goal use", async ({ page }) => {
  const api = await pwRequest.newContext({ baseURL: API });
  const suffix = Date.now();
  const workspaceName = `Workspace Autonomy ${suffix}`;
  const goalTitle = `Reach ${suffix} qualified users`;
  const editedGoalTitle = `Reach ${suffix} activated users`;
  const unsavedGoalTitle = `Reach ${suffix} unsaved users`;
  let token = "";
  let workspaceId = "";
  let goalId = "";

  try {
    const login = await api.post("/api/v1/auth/login", {
      data: { email: "demo@manor.local", password: "manor-demo" },
    });
    expect(login.ok(), `login failed: ${login.status()}`).toBeTruthy();
    token = (await login.json()).access_token;
    const headers = { Authorization: `Bearer ${token}` };

    const workspaceResponse = await api.post("/api/v1/workspaces", {
      headers,
      data: { name: workspaceName, heartbeat_enabled: false },
    });
    expect(workspaceResponse.ok(), `workspace failed: ${workspaceResponse.status()}`).toBeTruthy();
    workspaceId = (await workspaceResponse.json()).id;
    const readWorkspace = async () => {
      try {
        const workspace = await api.get(`/api/v1/workspaces/${workspaceId}`, { headers });
        return workspace.ok() ? await workspace.json() : null;
      } catch {
        return null;
      }
    };

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

    const startTrigger = page.getByRole("button", { name: "Start autonomous runtime", exact: true });
    await expect(startTrigger).toBeVisible();
    await startTrigger.focus();
    const autonomyDialog = page.getByRole("dialog", { name: "Start autonomous runtime" });
    await expect(autonomyDialog).toBeHidden();
    await startTrigger.press("Enter");
    await expect(autonomyDialog).toBeVisible();
    await expect(autonomyDialog.locator("button:focus, a:focus, input:focus")).toHaveCount(1);
    await expect(autonomyDialog.getByText("No goals yet", { exact: true })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(autonomyDialog).toBeHidden();
    await expect(startTrigger).toBeFocused();

    const goalRefreshResponsePromise = page.waitForResponse((candidate) => {
      const url = new URL(candidate.url());
      return candidate.request().method() === "GET"
        && url.pathname.endsWith("/api/v1/goals")
        && url.searchParams.get("workspace_id") === workspaceId;
    });
    await startTrigger.hover();
    const goalRefreshResponse = await goalRefreshResponsePromise;
    expect(goalRefreshResponse.ok(), `Goal refresh failed: ${goalRefreshResponse.status()}`).toBeTruthy();
    await expect(autonomyDialog).toBeVisible();
    await expect(autonomyDialog.getByText("Goals", { exact: true })).toBeVisible();
    await expect(autonomyDialog.getByText("No goals yet", { exact: true })).toBeVisible();
    await expect(autonomyDialog.getByRole("textbox")).toHaveCount(0);
    await autonomyDialog.getByRole("button", { name: "Add Goal", exact: true }).click();
    const goalInput = autonomyDialog.getByRole("textbox", { name: "Goal title" });
    await expect(goalInput).toBeVisible();
    await expect(autonomyDialog.getByRole("menuitem")).toHaveCount(0);
    await goalInput.fill(goalTitle);
    const createGoalRequestPromise = page.waitForRequest((candidate) => (
      candidate.method() === "POST"
      && candidate.url().endsWith("/api/v1/goals")
    ));
    const createGoalResponsePromise = page.waitForResponse((candidate) => (
      candidate.request().method() === "POST"
      && candidate.url().endsWith("/api/v1/goals")
    ));
    await autonomyDialog.getByRole("button", { name: "Save Goal", exact: true }).click();
    const createGoalRequest = await createGoalRequestPromise;
    const createGoalResponse = await createGoalResponsePromise;
    expect(
      createGoalResponse.ok(),
      `create Goal failed: ${createGoalResponse.status()} ${await createGoalResponse.text()}`,
    ).toBeTruthy();
    expect(createGoalRequest.postDataJSON()).toEqual({
      workspace_id: workspaceId,
      title: goalTitle,
      target_value: 1,
    });
    goalId = (await createGoalResponse.json()).id;
    await expect(autonomyDialog.getByText(goalTitle, { exact: true })).toBeVisible();
    await expect(
      autonomyDialog.getByRole("button", { name: "Add Goal", exact: true }),
    ).toBeFocused();

    const startRequestPromise = page.waitForRequest((candidate) => (
      candidate.method() === "POST"
      && candidate.url().includes(`/api/v1/workspaces/${workspaceId}/resume`)
    ));
    const startResponsePromise = page.waitForResponse((candidate) => (
      candidate.request().method() === "POST"
      && candidate.url().includes(`/api/v1/workspaces/${workspaceId}/resume`)
    ));
    await autonomyDialog.getByRole("button", { name: "Start autonomous runtime", exact: true }).click();
    const startRequest = await startRequestPromise;
    const startResponse = await startResponsePromise;
    expect(
      startResponse.ok(),
      `start failed: ${startResponse.status()} ${startResponse.url()} ${await startResponse.text()}`,
    ).toBeTruthy();
    expect(startRequest.postData()).toBeNull();

    await expect(autonomyDialog).toBeHidden();
    await expect.poll(async () => {
      const payload = await readWorkspace();
      if (!payload) return null;
      return {
        status: payload.status,
        heartbeat: payload.heartbeat_enabled,
        useGoals: payload.operating_model?.strategist?.use_goals,
      };
    }).toEqual({ status: "active", heartbeat: true, useGoals: true });
    const pauseTrigger = page.getByRole("button", { name: "Pause Workspace automation", exact: true });
    await expect(pauseTrigger).toBeVisible();

    const goalsResponse = await api.get(`/api/v1/goals?workspace_id=${workspaceId}`, { headers });
    expect(goalsResponse.ok(), `goals failed: ${goalsResponse.status()}`).toBeTruthy();
    const goalsPayload = await goalsResponse.json();
    const persistedGoal = (goalsPayload.items || goalsPayload).find(
      (goal: { title?: string }) => goal.title === goalTitle,
    );
    expect(goalId).not.toBe("");
    expect(persistedGoal?.id).toBe(goalId);
    expect(persistedGoal?.title).toBe(goalTitle);
    expect(persistedGoal?.target_value).toBe(1);

    await page.mouse.move(0, 0);
    await expect(page.getByText("Workspace resumed", { exact: true })).toBeHidden();
    await pauseTrigger.hover();
    const runningAutonomyDialog = page.getByRole("dialog", {
      name: "Pause Workspace automation",
    });
    await expect(runningAutonomyDialog).toBeVisible();
    await expect(runningAutonomyDialog.getByText(goalTitle, { exact: true })).toBeVisible();
    const runningGoalEditButton = runningAutonomyDialog.getByRole("button", {
      name: `Edit Goal: ${goalTitle}`,
    });
    await runningGoalEditButton.click();
    await expect(runningAutonomyDialog.getByRole("textbox", { name: "Goal title" })).toHaveValue(
      goalTitle,
    );
    await runningAutonomyDialog.getByRole("textbox", { name: "Goal title" }).press("Escape");
    await expect(runningGoalEditButton).toBeFocused();
    await runningAutonomyDialog.getByRole("button", {
      name: "Pause Workspace automation",
      exact: true,
    }).click();
    const resumeTrigger = page.getByRole("button", {
      name: "Resume Workspace automation",
      exact: true,
    });
    await expect(resumeTrigger).toBeVisible();
    await expect.poll(async () => {
      const payload = await readWorkspace();
      if (!payload) return null;
      return { status: payload.status, heartbeat: payload.heartbeat_enabled };
    }).toEqual({ status: "paused", heartbeat: false });
    await page.mouse.move(0, 0);
    await expect(page.getByText("Workspace paused", { exact: true })).toBeHidden();

    await resumeTrigger.hover();
    const resumeDialog = page.getByRole("dialog", { name: "Resume Workspace automation" });
    await expect(resumeDialog).toBeVisible();
    const existingGoalEditButton = resumeDialog.getByRole("button", {
      name: `Edit Goal: ${goalTitle}`,
    });
    await existingGoalEditButton.click();
    const existingGoalInput = resumeDialog.getByRole("textbox", { name: "Goal title" });
    await expect(existingGoalInput).toHaveValue(goalTitle);
    await existingGoalInput.press("Escape");
    await expect(existingGoalEditButton).toBeFocused();
    await existingGoalEditButton.click();
    await expect(existingGoalInput).toBeFocused();
    await existingGoalInput.fill(editedGoalTitle);
    const existingGoalRequestPromise = page.waitForRequest((candidate) => (
      candidate.method() === "PUT"
      && candidate.url().endsWith(`/api/v1/goals/${goalId}`)
    ));
    const existingGoalResponsePromise = page.waitForResponse((candidate) => (
      candidate.request().method() === "PUT"
      && candidate.url().endsWith(`/api/v1/goals/${goalId}`)
    ));
    await resumeDialog.getByRole("button", { name: "Save Goal", exact: true }).click();
    const existingGoalRequest = await existingGoalRequestPromise;
    const existingGoalResponse = await existingGoalResponsePromise;
    expect(
      existingGoalResponse.ok(),
      `edit Goal failed: ${existingGoalResponse.status()} ${await existingGoalResponse.text()}`,
    ).toBeTruthy();
    expect(existingGoalRequest.postDataJSON()).toEqual({ title: editedGoalTitle });
    await expect(resumeDialog.getByText(editedGoalTitle, { exact: true })).toBeVisible();
    await expect(
      resumeDialog.getByRole("button", { name: `Edit Goal: ${editedGoalTitle}` }),
    ).toBeFocused();

    const existingStartRequestPromise = page.waitForRequest((candidate) => (
      candidate.method() === "POST"
      && candidate.url().includes(`/api/v1/workspaces/${workspaceId}/resume`)
    ));
    const existingStartResponsePromise = page.waitForResponse((candidate) => (
      candidate.request().method() === "POST"
      && candidate.url().includes(`/api/v1/workspaces/${workspaceId}/resume`)
    ));
    await resumeDialog.getByRole("button", {
      name: "Resume Workspace automation",
      exact: true,
    }).click();
    const existingStartRequest = await existingStartRequestPromise;
    const existingStartResponse = await existingStartResponsePromise;
    expect(
      existingStartResponse.ok(),
      `existing Goal start failed: ${existingStartResponse.status()} ${await existingStartResponse.text()}`,
    ).toBeTruthy();
    expect(existingStartRequest.postData()).toBeNull();
    await expect.poll(async () => {
      const payload = await readWorkspace();
      if (!payload) return null;
      return {
        status: payload.status,
        heartbeat: payload.heartbeat_enabled,
        useGoals: payload.operating_model?.strategist?.use_goals,
      };
    }).toEqual({ status: "active", heartbeat: true, useGoals: true });
    await expect.poll(async () => {
      const response = await api.get(`/api/v1/goals/${goalId}`, { headers });
      return response.ok() ? (await response.json()).title : null;
    }).toBe(editedGoalTitle);

    await page.mouse.move(0, 0);
    await expect(page.getByText("Workspace resumed", { exact: true })).toBeHidden();
    const activePauseTrigger = page.getByRole("button", {
      name: "Pause Workspace automation",
      exact: true,
    });
    await activePauseTrigger.hover();
    const activePauseDialog = page.getByRole("dialog", {
      name: "Pause Workspace automation",
    });
    await expect(activePauseDialog.getByText(editedGoalTitle, { exact: true })).toBeVisible();
    await activePauseDialog.getByRole("button", {
      name: "Pause Workspace automation",
      exact: true,
    }).click();
    await expect.poll(async () => {
      const payload = await readWorkspace();
      if (!payload) return null;
      return { status: payload.status, heartbeat: payload.heartbeat_enabled };
    }).toEqual({ status: "paused", heartbeat: false });
    await page.mouse.move(0, 0);
    await expect(page.getByText("Workspace paused", { exact: true })).toBeHidden();

    await resumeTrigger.hover();
    await expect(resumeDialog).toBeVisible();
    await resumeDialog.getByRole("button", { name: `Edit Goal: ${editedGoalTitle}` }).click();
    await expect(resumeDialog.getByRole("textbox", { name: "Goal title" })).toHaveValue(
      editedGoalTitle,
    );
    await resumeDialog.getByRole("textbox", { name: "Goal title" }).fill(unsavedGoalTitle);
    await page.mouse.move(0, 0);
    await page.waitForTimeout(200);
    await expect(resumeDialog).toBeVisible();
    await expect(resumeDialog.getByRole("textbox", { name: "Goal title" })).toHaveValue(
      unsavedGoalTitle,
    );
    const deleteGoalRequestPromise = page.waitForRequest((candidate) => (
      candidate.method() === "DELETE"
      && candidate.url().endsWith(`/api/v1/goals/${goalId}`)
    ));
    const deleteGoalResponsePromise = page.waitForResponse((candidate) => (
      candidate.request().method() === "DELETE"
      && candidate.url().endsWith(`/api/v1/goals/${goalId}`)
    ));
    await resumeDialog.getByRole("button", { name: "Delete Goal", exact: true }).click();
    const deleteGoalDialog = page.getByRole("dialog", { name: "Delete Goal" });
    await expect(deleteGoalDialog).toBeVisible();
    await expect(deleteGoalDialog).toContainText(`Delete “${editedGoalTitle}”?`);
    await deleteGoalDialog.getByRole("button", { name: "Delete", exact: true }).click();
    const deleteGoalRequest = await deleteGoalRequestPromise;
    const deleteGoalResponse = await deleteGoalResponsePromise;
    expect(deleteGoalResponse.ok(), `delete Goal failed: ${deleteGoalResponse.status()}`).toBeTruthy();
    expect(deleteGoalRequest.postData()).toBeNull();
    await expect(deleteGoalDialog).toBeHidden();
    goalId = "";
    await page.setViewportSize({ width: 344, height: 458 });
    await page.route("**/api/v1/goals?*", async (route) => {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Goal service unavailable" }),
      });
    });
    await page.reload();
    await page.getByRole("button", { name: "Resume Workspace automation", exact: true }).click();
    const restartDialog = page.getByRole("dialog", { name: "Resume Workspace automation" });
    await expect(restartDialog).toBeVisible();
    await expect(restartDialog.getByText("Could not load Workspace Goals", { exact: true })).toBeVisible();
    await expect(restartDialog.getByRole("textbox")).toHaveCount(0);
    const restartDialogBox = await restartDialog.boundingBox();
    expect(restartDialogBox).not.toBeNull();
    expect(restartDialogBox!.x).toBeGreaterThanOrEqual(0);
    expect(restartDialogBox!.x + restartDialogBox!.width).toBeLessThanOrEqual(344);
    expect(restartDialogBox!.y).toBeGreaterThanOrEqual(0);
    expect(restartDialogBox!.y + restartDialogBox!.height).toBeLessThanOrEqual(458);
    await expect(
      restartDialog.getByRole("button", { name: "Resume Workspace automation", exact: true }),
    ).toBeEnabled();
    const restartRequestPromise = page.waitForRequest((candidate) => (
      candidate.method() === "POST"
      && candidate.url().includes(`/api/v1/workspaces/${workspaceId}/resume`)
    ));
    const restartResponsePromise = page.waitForResponse((candidate) => (
      candidate.request().method() === "POST"
      && candidate.url().includes(`/api/v1/workspaces/${workspaceId}/resume`)
    ));
    await restartDialog.getByRole("button", {
      name: "Resume Workspace automation",
      exact: true,
    }).click();
    const restartRequest = await restartRequestPromise;
    const restartResponse = await restartResponsePromise;
    expect(
      restartResponse.ok(),
      `restart failed: ${restartResponse.status()} ${restartResponse.url()} ${await restartResponse.text()}`,
    ).toBeTruthy();
    expect(restartRequest.postData()).toBeNull();
    await expect(restartDialog).toBeHidden();

    await expect.poll(async () => {
      const payload = await readWorkspace();
      if (!payload) return null;
      return {
        status: payload.status,
        heartbeat: payload.heartbeat_enabled,
        useGoals: payload.operating_model?.strategist?.use_goals,
      };
    }).toEqual({ status: "active", heartbeat: true, useGoals: false });
  } finally {
    const headers = token ? { Authorization: `Bearer ${token}` } : undefined;
    if (goalId && headers) await api.delete(`/api/v1/goals/${goalId}`, { headers });
    if (workspaceId && headers) await api.delete(`/api/v1/workspaces/${workspaceId}`, { headers });
    await api.dispose();
  }
});
