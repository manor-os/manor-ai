import { expect, test, type Page } from "@playwright/test";
import type { Workspace } from "../src/lib/types";

const WORKSPACE_ID = "workspace-delete-permission";

type WorkspaceFixture = Pick<Workspace, "id" | "entity_id" | "name" | "settings"> & {
  status: "active";
  kind: "standard";
  heartbeat_enabled: false;
  can_delete: boolean;
};

async function installWorkspaceDeleteMock(page: Page, workspace: WorkspaceFixture) {
  const deletedWorkspaceIds: string[] = [];
  const pageErrors: string[] = [];
  let canDelete = workspace.can_delete;
  const currentWorkspace = () => ({ ...workspace, can_delete: canDelete });

  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.addInitScript(() => {
    window.localStorage.setItem("manor_token", "workspace-delete-permission-token");
    window.localStorage.setItem("manor_locale", "en");
    window.localStorage.setItem("manor_tour_completed", "true");
    window.localStorage.setItem("manor_consent_v1", JSON.stringify({
      v: 1,
      ts: "2026-10-05T00:00:00.000Z",
      locale: "en",
      categories: { functional: false, analytics: false, marketing: false },
    }));
  });

  await page.route("**/config", (route) => route.fulfill({ json: {
    deployment_mode: "oss",
    environment: "e2e",
    email_enabled: false,
    fs_enabled: false,
    flows_available: false,
    flows_released: false,
    ai_credits_unlimited: true,
    support_tickets_enabled: false,
  } }));
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;

    if (path === "/api/v1/auth/me") {
      await route.fulfill({ json: {
        id: "workspace-delete-permission-user",
        username: "workspace-delete-permission-user",
        display_name: "Workspace Delete Permission User",
        email: "workspace-delete-permission@example.test",
        entity_id: workspace.entity_id,
        role: "member",
        locale: "en",
      } });
      return;
    }
    if (path === "/api/v1/workspaces" && request.method() === "GET") {
      await route.fulfill({ json: [currentWorkspace()] });
      return;
    }
    if (path === `/api/v1/workspaces/${workspace.id}` && request.method() === "DELETE") {
      deletedWorkspaceIds.push(workspace.id);
      await route.fulfill({ status: 204 });
      return;
    }
    if (path === `/api/v1/workspaces/${workspace.id}/pause` && request.method() === "POST") {
      await route.fulfill({ json: { ...currentWorkspace(), status: "paused" } });
      return;
    }
    if (path === "/api/v1/workspace-drafts" || path === "/api/v1/workspace-drafts/active") {
      await route.fulfill({ json: [] });
      return;
    }
    if (path === "/api/v1/workspaces/trash/grace-days") {
      await route.fulfill({ json: { grace_days: 30 } });
      return;
    }
    if (path === "/api/v1/notifications") {
      await route.fulfill({ json: { items: [], total: 0, unread_count: 0 } });
      return;
    }
    if (path === "/api/v1/platform/flags") {
      await route.fulfill({ json: { flags: {} } });
      return;
    }
    if (path === "/api/v1/admin/preferences") {
      await route.fulfill({ json: {} });
      return;
    }
    if (
      path === "/api/v1/agents/catalog" || path === "/api/v1/skills/catalog"
      || path === "/api/v1/agents" || path === "/api/v1/chat/conversations"
      || path === "/api/v1/auth/users" || path === "/api/v1/auth/users/directory"
      || path === "/api/v1/people/directory" || path === "/api/v1/staff"
      || path === "/api/v1/staff/roles" || path === "/api/v1/tasks" || path === "/api/v1/goals"
    ) {
      await route.fulfill({ json: [] });
      return;
    }
    await route.fulfill({ json: [] });
  });

  return {
    deletedWorkspaceIds,
    pageErrors,
    revokeDeletePermission: () => { canDelete = false; },
  };
}

function workspace(canDelete: boolean): WorkspaceFixture {
  return {
    id: WORKSPACE_ID,
    entity_id: "workspace-delete-permission-entity",
    name: "Workspace delete permission fixture",
    status: "active",
    kind: "standard",
    heartbeat_enabled: false,
    settings: {},
    can_delete: canDelete,
  };
}

test("Workspace list only exposes and submits delete for a server-authorized record", async ({ page }) => {
  const denied = await installWorkspaceDeleteMock(page, workspace(false));
  await page.goto("/workspaces");
  await expect(page.getByText("Workspace delete permission fixture", { exact: true })).toBeVisible();
  await expect(page.getByTitle("Delete", { exact: true })).toHaveCount(0);
  expect(denied.deletedWorkspaceIds).toEqual([]);
  expect(denied.pageErrors).toEqual([]);

  const allowedPage = await page.context().newPage();
  const allowed = await installWorkspaceDeleteMock(allowedPage, workspace(true));
  await allowedPage.goto("/workspaces");
  await allowedPage.getByTitle("Delete", { exact: true }).click();
  await expect(allowedPage.getByRole("dialog")).toBeVisible();
  await allowedPage.getByRole("button", { name: "Move to trash", exact: true }).click();
  await expect.poll(() => allowed.deletedWorkspaceIds).toEqual([WORKSPACE_ID]);
  expect(allowed.pageErrors).toEqual([]);
});

test("Workspace delete confirmation closes without deleting after refreshed permission revocation", async ({ page }) => {
  const state = await installWorkspaceDeleteMock(page, workspace(true));
  await page.goto("/workspaces");
  await page.getByTitle("Delete", { exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();

  state.revokeDeletePermission();
  await page.getByTitle("Pause", { exact: true }).dispatchEvent("click");

  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(state.deletedWorkspaceIds).toEqual([]);
  expect(state.pageErrors).toEqual([]);
});
