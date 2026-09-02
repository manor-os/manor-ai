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

const SEED_COMPLETED_PLAN = String.raw`
import asyncio
import json
import sys

from packages.core.database import async_session
from packages.core.models.execution import ExecutionPlan, ExecutionStep

payload = json.load(sys.stdin)

async def main():
    async with async_session() as db:
        plan = ExecutionPlan(
            entity_id=payload["entity_id"],
            workspace_id=payload["workspace_id"],
            task_id=payload["task_id"],
            status="completed",
            approval_required=False,
            execution_mode="live",
            plan_dag={"steps": [{"key": "create_csv", "kind": "code"}]},
        )
        db.add(plan)
        await db.flush()
        db.add(ExecutionStep(
            plan_id=plan.id,
            entity_id=payload["entity_id"],
            workspace_id=payload["workspace_id"],
            step_key="create_csv",
            kind="code",
            params={},
            depends_on=[],
            result={
                "artifact_materialized": True,
                "files": [{
                    "name": payload["filename"],
                    "url": f"/viewer/{payload['document_id']}",
                }],
            },
            step_status="done",
            attempt_count=1,
            max_attempts=3,
        ))
        await db.commit()

asyncio.run(main())
`;

const ASSERT_PROVENANCE = String.raw`
import asyncio
import json
import sys

from packages.core.database import async_session
from packages.core.models.document import Document

payload = json.load(sys.stdin)

async def main():
    async with async_session() as db:
        document = await db.get(Document, payload["document_id"])
        origin = (document.metadata_ or {}).get("origin", {}) if document else {}
        if origin.get("task_id") != payload["task_id"]:
            raise RuntimeError("Document task provenance was not backfilled")
        print("ok")

asyncio.run(main())
`;

const CLEAN_PLAN = String.raw`
import asyncio
import json
import sys

from sqlalchemy import delete, select

from packages.core.database import async_session
from packages.core.models.execution import ExecutionPlan, ExecutionStep

payload = json.load(sys.stdin)

async def main():
    async with async_session() as db:
        plan_ids = list((await db.execute(
            select(ExecutionPlan.id).where(ExecutionPlan.task_id == payload["task_id"])
        )).scalars())
        if plan_ids:
            await db.execute(delete(ExecutionStep).where(ExecutionStep.plan_id.in_(plan_ids)))
            await db.execute(delete(ExecutionPlan).where(ExecutionPlan.id.in_(plan_ids)))
            await db.commit()

asyncio.run(main())
`;

test.skip(!RUN_DOCKER_E2E, "Set E2E_DOCKER_TASK_ARTIFACT=1 for the Docker live-stack test");

test("Task artifact records provenance and opens through its Document viewer", async ({ page }) => {
  const api = await pwRequest.newContext({ baseURL: API });
  const suffix = Date.now();
  const filename = `task-artifact-${suffix}.csv`;
  const taskTitle = `Generate task artifact ${suffix}`;
  let token = "";
  let entityId = "";
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
    entityId = auth.entity_id;
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

    runApiContainerPython(SEED_COMPLETED_PLAN, {
      entity_id: entityId,
      workspace_id: workspaceId,
      task_id: taskId,
      document_id: documentId,
      filename,
    });

    const reconciledResponse = await api.get(`/api/v1/tasks/${taskId}`, { headers });
    expect(reconciledResponse.ok(), `reconcile failed: ${reconciledResponse.status()}`).toBeTruthy();
    const reconciledTask = await reconciledResponse.json();
    const taskFile = reconciledTask.actual_output?.files?.find(
      (file: { name?: string }) => file.name === filename,
    );
    expect(taskFile, JSON.stringify(reconciledTask.actual_output)).toBeTruthy();
    expect(taskFile?.document_id).toBe(documentId);
    expect(taskFile?.viewer_url).toBe(`/viewer/${documentId}`);
    expect(taskFile?.open_url).toBe(`/viewer/${documentId}`);

    expect(runApiContainerPython(ASSERT_PROVENANCE, {
      task_id: taskId,
      document_id: documentId,
    })).toBe("ok");

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
    if (taskId) runApiContainerPython(CLEAN_PLAN, { task_id: taskId });
    if (token) {
      const headers = { Authorization: `Bearer ${token}` };
      if (taskId) await api.delete(`/api/v1/tasks/${taskId}`, { headers });
      if (documentId) await api.post(`/api/v1/documents/${documentId}/trash`, { headers });
      if (workspaceId) await api.delete(`/api/v1/workspaces/${workspaceId}`, { headers });
    }
    await api.dispose();
  }
});
