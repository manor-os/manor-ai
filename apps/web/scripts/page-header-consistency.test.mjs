import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const APP_PAGE_HEADER_CONTRACTS = new Map([
  ["Account.tsx", "<PageHeader"],
  ["Activity.tsx", "<PageHeader"],
  ["AdminOAuthClients.tsx", "<PageHeader"],
  ["AgentDashboard.tsx", "<PageHeader"],
  ["AgentDetail.tsx", "<PageHeader"],
  ["Agents.tsx", "<PageHeader"],
  ["Announcements.tsx", "<PageHeader"],
  ["ApiKeys.tsx", "<PageHeader"],
  ["Apps.tsx", "<PageHeader"],
  ["BlueprintDetail.tsx", "<PageHeader"],
  ["BrowserSessions.tsx", "<PageHeader"],
  ["ChatHistory.tsx", "<PageHeader"],
  ["CustomFields.tsx", "<PageHeader"],
  ["Dashboard.tsx", "<PageHeader"],
  ["DiagramStudio.tsx", "<PageHeader"],
  ["DocEditor.tsx", "<PageHeaderTitle"],
  ["FileViewer.tsx", "<PageHeaderTitle"],
  ["Flows.tsx", "<PageHeader"],
  ["GoalExplorer.tsx", "<PageHeader"],
  ["Integrations.tsx", "<PageHeader"],
  ["JobLogs.tsx", "<PageHeader"],
  ["Knowledge.tsx", "<PageHeader"],
  ["Memories.tsx", "<PageHeader"],
  ["MerchantDashboard.tsx", "<PageHeader"],
  ["Messages.tsx", "<PageHeader"],
  ["Notifications.tsx", "<PageHeader"],
  ["PurchaseSuccess.tsx", "<PageHeader"],
  ["QRCode.tsx", "<PageHeader"],
  ["RemoteCoding.tsx", "<PageHeader"],
  ["Reports.tsx", "<PageHeader"],
  ["ScheduledJobs.tsx", "<PageHeader"],
  ["SearchResults.tsx", "<PageHeader"],
  ["Settings.tsx", "<PageHeader"],
  ["SimulationReport.tsx", "<PageHeader"],
  ["Skills.tsx", "<PageHeader"],
  ["TaskCollections.tsx", "<PageHeader"],
  ["TaskDetail.tsx", "<PageHeader"],
  ["Tasks.tsx", "<PageHeader"],
  ["Users.tsx", "<PageHeader"],
  ["VideoEditor.tsx", "<PageHeaderTitle"],
  ["WebhookManager.tsx", "<PageHeader"],
  ["WorkspaceDetail.tsx", "<PageHeader"],
  ["WorkspaceDraftChat.tsx", "<PageHeader"],
  ["Workspaces.tsx", "<PageHeader"],
  ["commerce/CommerceHome.tsx", "<PageHeader"],
  ["team/TeamLayout.tsx", "<PageHeader"],
]);

const ADMIN_PAGE_FILES = [
  "AdminMarketplace.tsx",
  "AdminTeam.tsx",
  "AffiliateDetail.tsx",
  "Affiliates.tsx",
  "Announcements.tsx",
  "AuditLog.tsx",
  "BlueprintReviews.tsx",
  "ClientErrors.tsx",
  "Commissions.tsx",
  "CreditsUsage.tsx",
  "Flags.tsx",
  "Integrations.tsx",
  "Invites.tsx",
  "Models.tsx",
  "OpsDashboard.tsx",
  "Overview.tsx",
  "Plans.tsx",
  "Roles.tsx",
  "SupportTickets.tsx",
  "SystemHealth.tsx",
  "TenantDetail.tsx",
  "TenantsList.tsx",
  "WaitingList.tsx",
];

const PRIMARY_PAGE_BODY_GUTTER_CONTRACTS = new Map([
  ["Dashboard.tsx", 'boxSizing: "border-box",\n          padding: 0,'],
  ["Tasks.tsx", 'className="tasks-page-header" style={{ padding: 0 }}'],
  ["ScheduledJobs.tsx", 'gap: 16, minHeight: "100%", padding: 0'],
  ["Workspaces.tsx", 'className="workspaces-page relative z-10 flex h-full min-h-0 flex-col overflow-hidden"'],
  ["Knowledge.tsx", 'className="flex-1 min-w-0 flex flex-col overflow-hidden"'],
  ["team/TeamLayout.tsx", 'className="h-full flex flex-col overflow-hidden"'],
  ["Agents.tsx", 'padding: 0,\n        overflow: "hidden",\n        position: "relative"'],
  ["Flows.tsx", 'flexDirection: "column", padding: 0, overflow: "hidden"'],
  ["Integrations.tsx", 'padding: 0,\n        overflow: "hidden",\n        position: "relative"'],
  ["Skills.tsx", 'padding: 0,\n        overflow: "hidden",\n        position: "relative"'],
  ["Apps.tsx", 'flexDirection: "column", padding: 0, overflow: "hidden"'],
  ["Settings.tsx", 'padding: "8px 24px 24px"'],
]);

test("routed app pages use the shared page-title contract", async () => {
  for (const [file, contract] of APP_PAGE_HEADER_CONTRACTS) {
    const source = await readFile(new URL(`../src/pages/${file}`, import.meta.url), "utf8");
    assert.ok(source.includes(contract), `${file} must render ${contract}`);
    assert.doesNotMatch(source, /<PageHeader(?:\s|>)[\s\S]{0,180}\bflush\b/, `${file} must not override the app header gutter`);
  }
});

test("admin pages use the same shared page header", async () => {
  for (const file of ADMIN_PAGE_FILES) {
    const source = await readFile(new URL(`../src/admin/pages/${file}`, import.meta.url), "utf8");
    assert.ok(source.includes("<AdminPageHeader"), `${file} must render AdminPageHeader`);
    assert.ok(!source.includes("<h1"), `${file} must not define page-local h1 styles`);
  }
});

test("primary page bodies reuse the app-shell gutter", async () => {
  for (const [file, contract] of PRIMARY_PAGE_BODY_GUTTER_CONTRACTS) {
    const source = await readFile(new URL(`../src/pages/${file}`, import.meta.url), "utf8");
    assert.ok(source.includes(contract), `${file} must not add a second outer page gutter`);
  }
});

test("PageHeader owns typography, row placement, and app-shell positioning", async () => {
  const source = await readFile(
    new URL("../src/components/ui/PageHeader.tsx", import.meta.url),
    "utf8",
  );

  assert.ok(source.includes("page-header-title"));
  assert.ok(source.includes("md:text-[28px]"));
  assert.ok(source.includes("tracking-[-0.014em]"));
  assert.ok(source.includes("page-header-subtitle"));
  assert.ok(source.includes("page-header-meta"));
  assert.ok(source.includes("page-header-title m-0 flex h-10"));
  assert.ok(source.includes("page-header-subtitle mt-1 h-5"));
  assert.ok(source.includes("page-header-meta mt-1 flex h-6"));
  assert.ok(source.includes("{meta && ("));
  assert.ok(source.includes("2xl:flex-row"));
  assert.ok(source.includes("2xl:flex-nowrap"));
  assert.doesNotMatch(
    source,
    /groupClass\s*=\s*[\s\S]{0,180}md:flex-1/,
    "toolbar groups must not expand away from neighboring tabs",
  );
  assert.ok(source.includes("text-[13px]"));
  assert.ok(source.includes("text-[color:var(--text-muted)]"));
  assert.ok(source.includes("PageHeaderBoundary"));
  assert.ok(source.includes("createPortal(header, portalContext.target)"));
  assert.ok(source.includes('data-page-header-layout={portalContext && !inline ? "app" : "inline"}'));
  assert.ok(source.includes("px-3 pb-0 pt-3"));
  assert.ok(!source.includes("flush?:"));
  assert.ok(!source.includes("compactControls?:"));
});

test("Workspaces aligns its body with the shared header gutter", async () => {
  const source = await readFile(
    new URL("../src/pages/Workspaces.tsx", import.meta.url),
    "utf8",
  );

  assert.ok(source.includes('className="workspaces-page relative z-10 flex h-full min-h-0 flex-col overflow-hidden"'));
  assert.doesNotMatch(
    source,
    /<TabSwitcher[\s\S]{0,180}\s+wrap(?:\s|\/>)/,
    "workspace tabs must remain a compact single-row control",
  );
});

test("Tasks and Knowledge explain their purpose before live page stats", async () => {
  for (const file of ["Tasks.tsx", "Knowledge.tsx"]) {
    const source = await readFile(new URL(`../src/pages/${file}`, import.meta.url), "utf8");
    assert.match(source, /<PageHeader[\s\S]{0,180}subtitle=\{t\("page\.(?:tasks|knowledge)\.subtitle"\)\}[\s\S]{0,180}meta=/);
  }
});

test("Blueprint detail keeps its aligned content wide on large screens", async () => {
  const source = await readFile(
    new URL("../src/pages/BlueprintDetail.tsx", import.meta.url),
    "utf8",
  );

  assert.ok(source.includes('width: "100%", maxWidth: 1600'));
  assert.ok(!source.includes("maxWidth: 1240"));
});

test("AppLayout provides one canonical header slot before routed page content", async () => {
  const source = await readFile(
    new URL("../src/layouts/AppLayout.tsx", import.meta.url),
    "utf8",
  );

  assert.ok(source.includes("<PageHeaderBoundary>"));
  assert.ok(source.includes("app-route-content min-h-0 min-w-0 flex-1 overflow-auto"));
  assert.ok(source.includes('app-route-content--settings p-0" : "px-6 pb-6 pt-2"'));
});
