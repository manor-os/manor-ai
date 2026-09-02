import { execFileSync } from "node:child_process";
import { writeFile } from "node:fs/promises";

import { expect, request as pwRequest, test } from "@playwright/test";

// Docker live-stack run:
// E2E_DOCKER_WORKSPACE_DRAFT=1 \
// E2E_API=http://127.0.0.1:8010 npm run test:e2e -- chat-workspace-draft-live.spec.ts

const API = process.env.E2E_API ?? "http://localhost:8000";
const API_CONTAINER = process.env.E2E_API_CONTAINER ?? "manor-api";
const RUN_DOCKER_E2E = process.env.E2E_DOCKER_WORKSPACE_DRAFT === "1";

const CHECK_GREETING_WORKER = String.raw`
from packages.core.celery_app import celery_app
from packages.core.queues import queue_for_task

queue = queue_for_task('packages.core.tasks.ai_tasks.send_agent_greetings').value
workers = celery_app.control.inspect(timeout=3).active_queues() or {}
assert any(item['name'] == queue for queues in workers.values() for item in queues), (
    f'Live E2E requires a worker consuming the {queue} queue on the API broker'
)
print(f'Greeting queue ready: {queue}')
`;

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

// Cleanup is intentionally the only direct database operation in this test.
// The journey itself must enter through the browser's ordinary Chat composer.
const CLEAN_CHAT_DRAFT = String.raw`
import asyncio
import json
import sys

from sqlalchemy import delete, select

from packages.core.database import async_session
from packages.core.models.workspace_draft import WorkspaceDraft

payload = json.load(sys.stdin)

async def main():
    async with async_session() as db:
        workspace_name = payload.get("workspace_name")
        draft_ids = set()
        if workspace_name:
            drafts = (await db.execute(select(WorkspaceDraft).where(
                WorkspaceDraft.fields['name'].astext == workspace_name,
            ))).scalars().all()
            draft_ids.update(draft.id for draft in drafts)
        if draft_ids:
            await db.execute(delete(WorkspaceDraft).where(WorkspaceDraft.id.in_(draft_ids)))
        await db.commit()

asyncio.run(main())
`;

function parseJson(value: unknown): any {
  if (typeof value !== "string") return value;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

function findWorkspaceDraftArtifact(value: unknown): any | null {
  const parsed = parseJson(value);
  if (!parsed || typeof parsed !== "object") return null;
  if (parsed.artifact_kind === "workspace_draft" && parsed.draft_id) return parsed;
  for (const key of ["result", "output", "data", "content"]) {
    const nested = findWorkspaceDraftArtifact(parsed[key]);
    if (nested) return nested;
  }
  return null;
}

function artifactFromMessages(messages: any[]): any | null {
  for (const message of messages) {
    for (const call of message.tool_calls || []) {
      const artifact = findWorkspaceDraftArtifact(call.raw_result ?? call.result);
      if (artifact) return artifact;
    }
  }
  return null;
}

test.skip(!RUN_DOCKER_E2E, "Set E2E_DOCKER_WORKSPACE_DRAFT=1 for the Docker live-stack test");

test("ordinary Chat creates a measured Workspace and round-trips its Blueprint through the live API", async ({ page }, testInfo) => {
  test.setTimeout(600_000);

  const api = await pwRequest.newContext({ baseURL: API });
  const suffix = String(Date.now());
  const workspaceName = `AI SDE Readiness E2E ${suffix}`;
  const updatedPrimaryWork = `Run mock interviews and weekly readiness reviews in Chat ${suffix}`;
  const goalTitle = `Interview readiness ${suffix}`;
  const metricKey = "interview_readiness";
  const formula = "Mean of coding, ML design and behavioral rubric scores (0-5), divided by 5 times 100; missing assessments remain unmeasured.";
  const evidenceSource = "Coach-reviewed mock interview rubric and assessment report, manually recorded after each review.";
  const startPrompt = [
    `Create a team Workspace named "${workspaceName}" in this Chat.`,
    "It is for AI software engineer interview preparation with weekly mock interviews and readiness reviews.",
    "Its primary work is coaching the candidate and reviewing mock assessments.",
    "Add exactly two services, interview_coaching and mock_assessment; map both to the SAME existing active Agent, and use Chat as the only internal channel.",
    "Do not add external integrations, automations, staff assignments or generated Knowledge documents.",
    "Do not set a monthly credit cap.",
    "Do not choose a Goal for me; ask me one concise question to confirm the Goal before marking the draft ready. Preserve the creation panel's default automatic mode.",
  ].join(" ");
  const continuePrompt = [
    "Update this same Workspace draft.",
    `Set its primary work exactly to "${updatedPrimaryWork}".`,
    `Configure a Goal titled "${goalTitle}" with target "90%" and weekly cadence.`,
    `I confirm a custom manually recorded measurement with key "${metricKey}", name "Interview readiness score", value_type "percent", unit "percent" and window "latest".`,
    `Use this exact formula/description: "${formula}"`,
    `Use this exact evidence source: "${evidenceSource}"`,
    "No initial score or baseline is known. Do not substitute a task-completion metric or claim automatic scoring.",
    "I confirm this Goal/measurement contract; retain the same Agent for the two services and mark ready when complete. Preserve my runtime-mode switch selection.",
    "Keep Chat as the internal channel and keep the monthly credit cap unset.",
  ].join(" ");
  let token = "";
  let draftId = "";
  let conversationId = "";
  let workspaceId = "";
  const installedWorkspaceIds: string[] = [];
  const blueprintIds: string[] = [];
  const existingConversationIds = new Set<string>();

  const headers = () => ({ Authorization: `Bearer ${token}` });
  const listMessages = async () => {
    if (!conversationId) return [];
    try {
      const response = await api.get(`/api/v1/chat/conversations/${conversationId}/messages?limit=200`, {
        headers: headers(),
      });
      if (!response.ok()) return [];
      return response.json();
    } catch {
      // The Docker port-forward can reset one keep-alive connection while a
      // long Chat stream finishes. A poll miss is retryable; persisted state is
      // still authoritative.
      return [];
    }
  };

  try {
    runApiContainerPython(CHECK_GREETING_WORKER, {});
    const login = await api.post("/api/v1/auth/login", {
      data: { email: "demo@manor.local", password: "manor-demo" },
    });
    expect(login.ok(), `login failed: ${login.status()}`).toBeTruthy();
    const auth = await login.json();
    token = auth.access_token;
    const existing = await api.get("/api/v1/chat/conversations", { headers: headers() });
    expect(existing.ok()).toBeTruthy();
    for (const conversation of await existing.json()) existingConversationIds.add(conversation.id);
    await page.route("**/api/v1/chat/stream", async (route) => {
      const body = route.request().postData() || "";
      if ([...existingConversationIds].some((id) => body.includes(id))) {
        await route.abort();
        throw new Error("E2E refused to send into a pre-existing conversation");
      }
      await route.continue(); // Guard only: no mocked response or request rewrite.
    });

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
    await page.setViewportSize({ width: 1440, height: 960 });
    // Use the app's explicit new-session identity so initial history restore
    // cannot race the New Chat click and attach this test to another session.
    await page.goto(`/chat?conversation=${encodeURIComponent(`manor-new:e2e-${suffix}`)}`);

    const composer = page.getByRole("main").getByRole("textbox");
    await expect(composer).toBeVisible();
    await composer.fill(startPrompt);
    await composer.press("Enter");

    // The first ordinary Chat request must persist a real conversation and a
    // real start_workspace_draft/manor action result before the UI can project it.
    await expect.poll(async () => {
      let response;
      try {
        response = await api.get("/api/v1/chat/conversations", { headers: headers() });
      } catch {
        return "";
      }
      if (!response.ok()) return "";
      const conversations = await response.json();
      for (const item of conversations) {
        if (existingConversationIds.has(item.id)) continue;
        let messagesResponse;
        try {
          messagesResponse = await api.get(`/api/v1/chat/conversations/${item.id}/messages?limit=200`, {
            headers: headers(),
          });
        } catch {
          continue;
        }
        if (!messagesResponse.ok()) continue;
        const persistedMessages = await messagesResponse.json();
        if (persistedMessages.some((message: any) => message.role === "user" && message.content === startPrompt)) {
          conversationId = item.id;
          break;
        }
      }
      return conversationId;
    }, { timeout: 240_000, intervals: [1_000, 2_000, 5_000] }).not.toBe("");

    await expect.poll(async () => {
      const artifact = artifactFromMessages(await listMessages());
      draftId = artifact?.draft_id || "";
      return draftId;
    }, { timeout: 240_000, intervals: [2_000, 5_000] }).not.toBe("");

    // The first turn must stop at the conversational confirmation boundary;
    // silence is not "no Goal"; automatic mode is the product default.
    await expect(composer).toHaveAttribute("aria-disabled", "false", { timeout: 240_000 });
    const initialResponse = await api.get(`/api/v1/workspace-drafts/${draftId}`, { headers: headers() });
    expect(initialResponse.ok()).toBeTruthy();
    const initialDraft = await initialResponse.json();
    await writeFile(testInfo.outputPath("unconfirmed-draft.json"), JSON.stringify(initialDraft, null, 2));
    // Soft assertions keep a violated confirmation boundary RED while still
    // collecting independent evidence for metric creation and portability.
    expect.soft(initialDraft.ready, "an unconfirmed draft must not be ready").toBe(false);
    expect.soft(initialDraft.fields._creation_preferences).toEqual({
      goal_confirmed: false, autonomy_confirmed: false,
    });
    expect.soft(initialDraft.missing).toContain("creation_preferences");
    expect.soft(initialDraft.fields.heartbeat_enabled).toBe(true);

    const artifactCard = page.locator("button.chat-artifact-summary-open", { hasText: workspaceName }).last();
    await expect(artifactCard).toBeVisible({ timeout: 240_000 });
    await artifactCard.click();

    const panel = page.getByRole("region", { name: "Draft summary" });
    await expect(panel).toBeVisible();
    await expect(panel.getByText("No monthly credit cap", { exact: true })).toBeVisible();
    const runtimeMode = panel.getByRole("switch", { name: "Run automatically after creation" });
    await expect(runtimeMode).toBeChecked();
    await runtimeMode.click();
    await expect(runtimeMode).not.toBeChecked();
    if (!initialDraft.ready) {
      await expect(panel.getByRole("button", { name: "Keep chatting until ready" })).toBeDisabled();
    }

    // Keep the Draft usable at the narrowest two-pane width. This reproduces
    // the real Chat shell with its navigation rail, where the workbench has
    // about 729px and the output pane must not collapse beneath its footer.
    await page.setViewportSize({ width: 1001, height: 998 });
    const narrowLayout = await panel.evaluate((element) => {
      const body = element.querySelector<HTMLElement>(".workspace-draft-panel__body");
      const header = element.querySelector<HTMLElement>(".workspace-draft-panel__header");
      const progress = element.querySelector<HTMLElement>(".workspace-draft-progress");
      const footer = element.querySelector<HTMLElement>(".workspace-draft-panel__footer");
      if (!body || !header || !progress || !footer) return null;
      const panelRect = element.getBoundingClientRect();
      const bodyRect = body.getBoundingClientRect();
      const headerRect = header.getBoundingClientRect();
      const progressRect = progress.getBoundingClientRect();
      const footerRect = footer.getBoundingClientRect();
      return {
        panelWidth: panelRect.width,
        bodyHeight: bodyRect.height,
        bodyClientWidth: body.clientWidth,
        bodyScrollWidth: body.scrollWidth,
        bodyBottom: bodyRect.bottom,
        headerBottom: headerRect.bottom,
        progressRight: progressRect.right,
        panelRight: panelRect.right,
        footerTop: footerRect.top,
      };
    });
    expect(narrowLayout).not.toBeNull();
    expect(narrowLayout!.panelWidth).toBeGreaterThanOrEqual(320);
    expect(narrowLayout!.bodyHeight).toBeGreaterThan(300);
    expect(narrowLayout!.bodyScrollWidth).toBeLessThanOrEqual(narrowLayout!.bodyClientWidth);
    expect(narrowLayout!.headerBottom).toBeLessThanOrEqual(narrowLayout!.bodyBottom + 1);
    expect(narrowLayout!.progressRight).toBeLessThanOrEqual(narrowLayout!.panelRight);
    expect(narrowLayout!.footerTop).toBeGreaterThanOrEqual(narrowLayout!.bodyBottom - 1);

    await expect(composer).toHaveAttribute("aria-disabled", "false");
    await composer.fill(continuePrompt);
    await composer.press("Enter");

    // The second browser send is only considered complete once the Draft's own
    // transcript and fields prove continue_workspace_draft ran and committed.
    await expect.poll(async () => {
      let response;
      try {
        response = await api.get(`/api/v1/workspace-drafts/${draftId}`, { headers: headers() });
      } catch {
        return "";
      }
      if (!response.ok()) return "";
      const persisted = await response.json();
      const draftUserTurns = (persisted.messages || []).filter(
        (message: any) => message.role === "user",
      );
      const latestDraftPrompt = String(draftUserTurns.at(-1)?.content || "");
      const chatHasOriginalRequest = (await listMessages()).some(
        (message: any) => message.role === "user" && message.content === continuePrompt,
      );
      const hasContinuedTurn =
        draftUserTurns.length >= 2 && latestDraftPrompt.includes(updatedPrimaryWork);
      return chatHasOriginalRequest && hasContinuedTurn
        ? persisted.fields?.primary_work || ""
        : "";
    }, { timeout: 240_000, intervals: [2_000, 5_000] }).toBe(updatedPrimaryWork);

    await expect.poll(async () => {
      const response = await api.get(
        `/api/v1/workspace-drafts/${draftId}`,
        { headers: headers() },
      ).catch(() => null);
      if (!response?.ok()) return false;
      const persisted = await response.json();
      const preferences = persisted.fields?._creation_preferences || {};
      const goal = (persisted.fields?.goals || []).find(
        (item: any) => item.title === goalTitle,
      );
      return (
        persisted.ready === true
        && persisted.status === "ready"
        && preferences.goal_confirmed === true
        && preferences.autonomy_confirmed === true
        && persisted.fields?.heartbeat_enabled === false
        && goal?.target === "90%"
        && goal?.cadence === "weekly"
        && goal?.stat_key === metricKey
        && goal?.measurement?.description === formula
        && goal?.measurement?.source === evidenceSource
      );
    }, { timeout: 240_000, intervals: [2_000, 5_000] }).toBe(true);

    await expect(panel.getByText(updatedPrimaryWork, { exact: true })).toBeVisible({ timeout: 60_000 });
    await expect(panel.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "100");
    await expect(panel.getByText(formula, { exact: true })).toBeVisible();
    await expect(panel.getByText(evidenceSource, { exact: true })).toBeVisible();
    await expect(panel.getByText("Manual recording required · starts unmeasured", { exact: true })).toBeVisible();
    await panel.getByText(formula, { exact: true }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath("confirmed-readiness-draft.png") });

    const firstDetailBorders = await panel.locator(".workspace-draft-details > section").first().evaluate((element) => {
      const style = window.getComputedStyle(element);
      return [style.borderTopWidth, style.borderRightWidth, style.borderBottomWidth, style.borderLeftWidth];
    });
    expect(firstDetailBorders).toEqual(["0px", "0px", "0px", "0px"]);

    await page.getByRole("button", { name: "Close artifact" }).click();
    await expect(panel).toBeHidden();
    await artifactCard.click();
    await expect(panel).toBeVisible();

    const finalizeResponse = page.waitForResponse((response) =>
      response.url().includes(`/api/v1/workspace-drafts/${draftId}/finalize`)
      && response.request().method() === "POST",
    );
    await panel.getByRole("button", { name: "Create Workspace" }).click();
    const finalized = await finalizeResponse;
    expect(finalized.status()).toBe(200);
    workspaceId = (await finalized.json()).workspace_id;
    await expect(panel.getByRole("button", { name: "Open workspace", exact: true })).toBeVisible();
    expect(new URL(page.url()).pathname).toBe("/chat");

    const persistedDraft = await api.get(`/api/v1/workspace-drafts/${draftId}`, { headers: headers() });
    expect(persistedDraft.ok()).toBeTruthy();
    const persistedDraftBody = await persistedDraft.json();
    expect(persistedDraftBody.status).toBe("finalized");
    expect(persistedDraftBody.finalized_workspace_id).toBe(workspaceId);
    expect(persistedDraftBody.fields._creation_preferences).toEqual({
      goal_confirmed: true,
      autonomy_confirmed: true,
    });
    expect(persistedDraftBody.fields.heartbeat_enabled).toBe(false);
    expect(persistedDraftBody.fields.goals).toEqual(expect.arrayContaining([
      expect.objectContaining({
        title: goalTitle,
        target: "90%",
        cadence: "weekly",
      }),
    ]));

    const workspace = await api.get(`/api/v1/workspaces/${workspaceId}`, { headers: headers() });
    expect(workspace.ok()).toBeTruthy();
    const workspaceBody = await workspace.json();
    expect(workspaceBody.name).toBe(workspaceName);
    expect(workspaceBody.heartbeat_enabled).toBe(false);

    const goals = await api.get(
      `/api/v1/goals?workspace_id=${workspaceId}`,
      { headers: headers() },
    );
    expect(goals.ok()).toBeTruthy();
    const goalRows = await goals.json();
    expect(goalRows).toEqual(expect.arrayContaining([
      expect.objectContaining({
        title: goalTitle,
        target_value: 90,
        measurement_cadence: null,
        measurement_source: null,
        baseline_value: null,
        current_value: null,
      }),
    ]));
    const goal = goalRows.find((item: any) => item.title === goalTitle);
    const statsResponse = await api.get(`/api/v1/workspaces/${workspaceId}/stats`, { headers: headers() });
    expect(statsResponse.ok()).toBeTruthy();
    const stats = (await statsResponse.json()).items;
    expect(stats).toHaveLength(1);
    const stat = stats[0];
    expect(stat).toMatchObject({
      key: metricKey, description: formula, collector_type: "manual", current_value: null,
      collection_cadence: "weekly", collector_config: { source: evidenceSource },
    });
    expect(goal.stat_id).toBe(stat.id);

    const mappings = persistedDraftBody.fields.agent_mappings;
    expect(mappings).toHaveLength(2);
    expect(new Set(mappings.map((mapping: any) => mapping.agent_id))).toHaveProperty("size", 1);
    const greetingMessages = async () => {
      const response = await api.get(`/api/v1/workspaces/${workspaceId}/chat/messages`, { headers: headers() });
      expect(response.ok()).toBeTruthy();
      return (await response.json()).filter((message: any) => message.meta?.agent_greeting === true);
    };
    await expect.poll(async () => (await greetingMessages()).length, { timeout: 90_000 }).toBe(1);

    const observation = await api.post(`/api/v1/workspaces/${workspaceId}/stats/${stat.id}/observations`, {
      headers: headers(), data: { value: 65, note: `Synthetic E2E mock review ${suffix}; not a real candidate assessment.` },
    });
    expect(observation.status()).toBe(201);
    const measuredGoal = await api.get(`/api/v1/goals/${goal.id}`, { headers: headers() });
    expect((await measuredGoal.json()).current_value).toBe(65);

    const exportBlueprint = async (id: string, label: string) => {
      const response = await api.post(`/api/v1/workspaces/${id}/export-blueprint`, {
        headers: headers(), data: { slug: `readiness-e2e-${suffix}-${label}`, title: workspaceName, summary: "E2E confirmed readiness contract" },
      });
      expect(response.status(), await response.text()).toBe(201);
      const blueprint = await response.json();
      blueprintIds.push(blueprint.id);
      await writeFile(testInfo.outputPath(`${label}-blueprint.json`), JSON.stringify(blueprint.payload, null, 2));
      return blueprint;
    };
    const source = await exportBlueprint(workspaceId, "source");
    expect(source.payload.recipe.stats).toHaveLength(1);
    expect(source.payload.recipe.stats[0]).not.toHaveProperty("current_value");
    expect(source.payload.recipe.goals[0]).toMatchObject({ stat_key: metricKey, target_value: 90 });
    expect(source.payload.recipe.goals[0]).not.toHaveProperty("current_value");
    for (const mode of ["simulate", "live"]) {
      const install = await api.post(`/api/v1/blueprints/${source.id}/install`, {
        headers: headers(), data: { mode, workspace_name: `${workspaceName} ${mode}` },
      });
      expect(install.status(), await install.text()).toBe(201);
      const result = await install.json();
      installedWorkspaceIds.push(result.workspace_id);
      expect(result.todos.filter((todo: any) => todo.blocking)).toEqual([]);
      expect(result.stat_ids).toHaveLength(1);
      expect(result.goal_ids).toHaveLength(1);
      const installedGoal = await api.get(`/api/v1/goals/${result.goal_ids[0]}`, { headers: headers() });
      expect(await installedGoal.json()).toMatchObject({ stat_id: result.stat_ids[0], current_value: null });
      const installed = await exportBlueprint(result.workspace_id, mode);
      expect(installed.payload.recipe.stats).toEqual(source.payload.recipe.stats);
      expect(installed.payload.recipe.goals).toEqual(source.payload.recipe.goals);
    }
    expect(await greetingMessages()).toHaveLength(1);
    await testInfo.attach("measurement-evidence", {
      body: JSON.stringify({ workspaceId, goalId: goal.id, statId: stat.id, initialValue: null, recordedValue: 65, installedWorkspaceIds, blueprintIds }, null, 2),
      contentType: "application/json",
    });
  } finally {
    if (token && draftId) {
      const response = await api.get(`/api/v1/workspace-drafts/${draftId}`, { headers: headers() }).catch(() => null);
      if (response?.ok()) await writeFile(testInfo.outputPath("final-draft.json"), JSON.stringify(await response.json(), null, 2));
    }
    if (token && !workspaceId) {
      const workspaces = await api.get("/api/v1/workspaces", { headers: headers() }).catch(() => null);
      if (workspaces?.ok()) {
        const matchingWorkspace = (await workspaces.json()).find(
          (workspace: any) => workspace.name === workspaceName,
        );
        workspaceId = matchingWorkspace?.id || "";
      }
    }
    if (token) {
      for (const id of [...installedWorkspaceIds, workspaceId].filter(Boolean)) {
        await api.delete(`/api/v1/workspaces/${id}`, { headers: headers() }).catch(() => null);
      }
      for (const id of blueprintIds.reverse()) {
        await api.delete(`/api/v1/blueprints/${id}`, { headers: headers() }).catch(() => null);
      }
      if (conversationId && !existingConversationIds.has(conversationId)) {
        await api.delete(`/api/v1/chat/conversations/${conversationId}`, { headers: headers() }).catch(() => null);
      }
    }
    runApiContainerPython(CLEAN_CHAT_DRAFT, {
      workspace_name: workspaceName,
    });
    await api.dispose();
  }
});
