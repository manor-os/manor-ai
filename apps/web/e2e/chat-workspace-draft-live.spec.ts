import { execFileSync } from "node:child_process";

import { expect, request as pwRequest, test } from "@playwright/test";

// Docker live-stack run:
// E2E_DOCKER_WORKSPACE_DRAFT=1 \
// E2E_API=http://127.0.0.1:8010 npm run test:e2e -- chat-workspace-draft-live.spec.ts

const API = process.env.E2E_API ?? "http://localhost:8000";
const API_CONTAINER = process.env.E2E_API_CONTAINER ?? "manor-api";
const RUN_DOCKER_E2E = process.env.E2E_DOCKER_WORKSPACE_DRAFT === "1";

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
from packages.core.models.task import Conversation, Message
from packages.core.models.workspace_draft import WorkspaceDraft

payload = json.load(sys.stdin)

async def main():
    async with async_session() as db:
        conversation_id = payload.get("conversation_id")
        draft_id = payload.get("draft_id")
        workspace_name = payload.get("workspace_name")
        if conversation_id:
            await db.execute(delete(Message).where(Message.conversation_id == conversation_id))
            await db.execute(delete(Conversation).where(Conversation.id == conversation_id))
        draft_ids = {draft_id} if draft_id else set()
        if workspace_name:
            drafts = (await db.execute(select(WorkspaceDraft))).scalars().all()
            draft_ids.update(
                draft.id
                for draft in drafts
                if (draft.fields or {}).get("name") == workspace_name
            )
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

test("ordinary Chat starts, continues, and finalizes a Workspace draft through the live API", async ({ page }) => {
  test.setTimeout(360_000);

  const api = await pwRequest.newContext({ baseURL: API });
  const suffix = String(Date.now());
  const workspaceName = `Chat Workspace E2E ${suffix}`;
  const updatedPrimaryWork = `Run weekly planning and delivery in Chat ${suffix}`;
  const goalTitle = `Weekly delivery ${suffix}`;
  const startPrompt = [
    `Create a team Workspace named "${workspaceName}" in this Chat.`,
    "It is for weekly product planning with clear ownership and visible delivery progress.",
    "Its primary work is turning product requirements into accountable weekly delivery.",
    "Add a product_planning service, match an existing active Agent, and use Chat as the internal channel.",
    "Do not set a monthly credit cap.",
    "Do not choose a Goal or autonomous setting for me; ask me one concise question to confirm both before marking the draft ready.",
  ].join(" ");
  const continuePrompt = [
    "Update this same Workspace draft.",
    `Set its primary work exactly to "${updatedPrimaryWork}".`,
    `Configure a Goal titled "${goalTitle}" with target "90%" and weekly cadence.`,
    "Keep autonomous mode off after creation.",
    "Keep Chat as the internal channel and keep the monthly credit cap unset.",
  ].join(" ");
  let token = "";
  let draftId = "";
  let conversationId = "";
  let workspaceId = "";

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
    const login = await api.post("/api/v1/auth/login", {
      data: { email: "demo@manor.local", password: "manor-demo" },
    });
    expect(login.ok(), `login failed: ${login.status()}`).toBeTruthy();
    const auth = await login.json();
    token = auth.access_token;

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
    await page.goto("/chat");
    // `/chat` may restore the most recent persisted Manor conversation. Start
    // from the same explicit UI action a user would take so this run cannot
    // project an artifact card left behind by an earlier interrupted run.
    await page.getByRole("button", { name: "New Chat", exact: true }).click();

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
    // silence is neither "no Goal" nor "autonomous off".
    await expect.poll(async () => {
      const response = await api.get(
        `/api/v1/workspace-drafts/${draftId}`,
        { headers: headers() },
      ).catch(() => null);
      if (!response?.ok()) return false;
      const persisted = await response.json();
      const preferences = persisted.fields?._creation_preferences || {};
      const latestAssistant = [...(persisted.messages || [])]
        .reverse()
        .find((message: any) => message.role === "assistant");
      const reply = String(latestAssistant?.content || "");
      return (
        persisted.ready === false
        && persisted.status === "active"
        && (persisted.missing || []).includes("creation_preferences")
        && preferences.goal_confirmed === false
        && preferences.autonomy_confirmed === false
        && /goal/i.test(reply)
        && /autonomous/i.test(reply)
      );
    }, { timeout: 240_000, intervals: [2_000, 5_000] }).toBe(true);

    const artifactCard = page.locator("button.chat-artifact-summary-open", { hasText: workspaceName }).last();
    await expect(artifactCard).toBeVisible({ timeout: 240_000 });
    await artifactCard.click();

    const panel = page.getByRole("region", { name: "Draft summary" });
    await expect(panel).toBeVisible();
    await expect(panel.getByText("No monthly credit cap", { exact: true })).toBeVisible();
    await expect(
      panel.getByRole("button", { name: "Keep chatting until ready" }),
    ).toBeDisabled();

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
      );
    }, { timeout: 240_000, intervals: [2_000, 5_000] }).toBe(true);

    await expect(panel.getByText(updatedPrimaryWork, { exact: true })).toBeVisible({ timeout: 60_000 });
    await expect(panel.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "100");

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
    expect(await goals.json()).toEqual(expect.arrayContaining([
      expect.objectContaining({
        title: goalTitle,
        target_value: 90,
        measurement_cadence: "weekly",
      }),
    ]));
  } finally {
    if (token && !workspaceId) {
      const workspaces = await api.get("/api/v1/workspaces", { headers: headers() }).catch(() => null);
      if (workspaces?.ok()) {
        const matchingWorkspace = (await workspaces.json()).find(
          (workspace: any) => workspace.name === workspaceName,
        );
        workspaceId = matchingWorkspace?.id || "";
      }
    }
    if (token && workspaceId) {
      await api.delete(`/api/v1/workspaces/${workspaceId}`, { headers: headers() }).catch(() => null);
    }
    runApiContainerPython(CLEAN_CHAT_DRAFT, {
      draft_id: draftId,
      conversation_id: conversationId,
      workspace_name: workspaceName,
    });
    await api.dispose();
  }
});
