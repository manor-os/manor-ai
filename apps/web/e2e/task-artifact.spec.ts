import { execFileSync } from "node:child_process";

import { expect, request as pwRequest, test } from "@playwright/test";

// Docker live-stack run:
// E2E_DOCKER_TASK_ARTIFACT=1 E2E_SKIP_WEB_SERVER=1 \
// E2E_BASE=http://127.0.0.1:18080 E2E_API=http://127.0.0.1:8010 \
// npm run test:e2e -- task-artifact.spec.ts

const API = process.env.E2E_API ?? "http://localhost:8000";
const API_CONTAINER = process.env.E2E_API_CONTAINER ?? "manor-api";
const RUN_DOCKER_E2E = process.env.E2E_DOCKER_TASK_ARTIFACT === "1";

function runApiContainerPython(script: string, payload: Record<string, string>): string {
  return execFileSync(
    "docker",
    ["exec", "-i", API_CONTAINER, "python", "-c", script],
    {
      encoding: "utf8",
      input: JSON.stringify(payload),
      stdio: ["pipe", "pipe", "pipe"],
    },
  ).trim();
}

// Seed the Task's delivered output the way the agent loop records it.
const SEED_TASK_OUTPUT = String.raw`
import asyncio
import json
import sys

from packages.core.database import async_session
from packages.core.models.task import Task

payload = json.load(sys.stdin)

async def main():
    async with async_session() as db:
        task = await db.get(Task, payload["task_id"])
        task.actual_output = {
            "files": [{
                "name": payload["filename"],
                "document_id": payload["document_id"],
                "url": f"/viewer/{payload['document_id']}",
            }],
        }
        await db.commit()

asyncio.run(main())
`;

test.skip(!RUN_DOCKER_E2E, "Set E2E_DOCKER_TASK_ARTIFACT=1 for the Docker live-stack test");

test("Task output file opens through its Document viewer", async ({ page }) => {
  const api = await pwRequest.newContext({ baseURL: API });
  const suffix = Date.now();
  const filename = `task-artifact-${suffix}.csv`;
  const taskTitle = `Generate task artifact ${suffix}`;
  let token = "";
  let workspaceId = "";
  let taskId = "";
  let documentId = "";

  try {
    const login = await api.post("/api/v1/auth/login", {
      data: {
        email: "demo@manor.local",
        password: "manor-demo",
      },
    });
    expect(login.ok(), `login failed: ${login.status()}`).toBeTruthy();
    const auth = await login.json();
    token = auth.access_token;
    const headers = { Authorization: `Bearer ${token}` };

    const workspaceResponse = await api.post("/api/v1/workspaces", {
      headers,
      data: { name: `Task Artifact E2E ${suffix}` },
    });
    expect(workspaceResponse.ok(), `workspace failed: ${workspaceResponse.status()}`).toBeTruthy();
    workspaceId = (await workspaceResponse.json()).id;

    const taskResponse = await api.post("/api/v1/tasks", {
      headers,
      data: {
        title: taskTitle,
        workspace_id: workspaceId,
      },
    });
    expect(taskResponse.ok(), `task failed: ${taskResponse.status()}`).toBeTruthy();
    taskId = (await taskResponse.json()).id;

    const uploadResponse = await api.post("/api/v1/documents/upload", {
      headers,
      multipart: {
        file: {
          name: filename,
          mimeType: "text/csv",
          buffer: Buffer.from("label,value\ne2e-value,42\n", "utf8"),
        },
      },
    });
    expect(uploadResponse.ok(), `upload failed: ${uploadResponse.status()}`).toBeTruthy();
    documentId = (await uploadResponse.json()).id;

    runApiContainerPython(SEED_TASK_OUTPUT, {
      task_id: taskId,
      document_id: documentId,
      filename,
    });

    const seededResponse = await api.get(`/api/v1/tasks/${taskId}`, { headers });
    expect(seededResponse.ok(), `task read failed: ${seededResponse.status()}`).toBeTruthy();
    const seededTask = await seededResponse.json();
    const taskFile = seededTask.actual_output?.files?.find(
      (file: { name?: string }) => file.name === filename,
    );
    expect(taskFile, JSON.stringify(seededTask.actual_output)).toBeTruthy();
    expect(taskFile?.document_id).toBe(documentId);

    await page.addInitScript((accessToken) => {
      window.localStorage.setItem("manor_token", accessToken as string);
    }, token);
    await page.goto(`/tasks/${taskId}`);
    const artifactCard = page.getByText(filename, { exact: true }).first();
    await expect(artifactCard).toBeVisible();

    const documentResponse = page.waitForResponse(
      (response) => response.url().includes(`/api/v1/documents/${documentId}`)
        && response.request().method() === "GET",
    );
    await artifactCard.click();
    expect((await documentResponse).status()).toBe(200);
    await expect(page).toHaveURL(new RegExp(`/viewer/${documentId}(?:[?#].*)?$`));
    await expect(page.getByText("Document not found", { exact: true })).toHaveCount(0);

    await page.getByRole("button", { name: "Go Back", exact: true }).first().click();
    await expect(page).toHaveURL(new RegExp(`/tasks/${taskId}(?:[?#].*)?$`));
    await expect(page.getByText(filename, { exact: true }).first()).toBeVisible();

    await page.goto("/tasks");
    const rejectCookies = page.getByRole("button", { name: "Reject all", exact: true });
    if (await rejectCookies.isVisible()) await rejectCookies.click();
    const skipTour = page.getByRole("button", { name: "Skip", exact: true });
    if (await skipTour.isVisible()) await skipTour.click();
    await page.getByPlaceholder("Search tasks...").fill(taskTitle);
    const taskCard = page.getByRole("button", { name: new RegExp(`View task: ${taskTitle}`) });
    await expect(taskCard).toBeVisible();
    await taskCard.click();
    await expect(page).toHaveURL(new RegExp(`/tasks\\?[^#]*task=${taskId}(?:#.*)?$`));

    const drawerArtifactCard = page.getByText(filename, { exact: true }).first();
    await expect(drawerArtifactCard).toBeVisible();
    await drawerArtifactCard.click();
    await expect(page).toHaveURL(new RegExp(`/viewer/${documentId}(?:[?#].*)?$`));

    await page.getByRole("button", { name: "Go Back", exact: true }).first().click();
    await expect(page).toHaveURL(new RegExp(`/tasks\\?[^#]*task=${taskId}#task-output-file-`));
    await expect(page.getByText(filename, { exact: true }).first()).toBeVisible();
    await expect(page.getByRole("heading", { name: new RegExp(taskTitle) })).toBeVisible();
  } finally {
    if (token) {
      const headers = { Authorization: `Bearer ${token}` };
      if (taskId) await api.delete(`/api/v1/tasks/${taskId}`, { headers });
      if (documentId) await api.post(`/api/v1/documents/${documentId}/trash`, { headers });
      if (workspaceId) await api.delete(`/api/v1/workspaces/${workspaceId}`, { headers });
    }
    await api.dispose();
  }
});
