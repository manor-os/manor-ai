import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

import {
  api,
  type WorkspaceAppDataBlockResult,
  type WorkspaceAppDataComponent,
  type WorkspaceAppDataContextSummary,
  type WorkspaceAppDataContextsResponse,
  type WorkspaceAppDataItem,
  type WorkspaceAppDataResponse,
  type WorkspaceAppSectionData,
} from "../../src/lib/api";
import WorkspaceAppView from "../../src/components/workspaces/WorkspaceAppView";
import { useConfigStore } from "../../src/stores/config";
import "../../src/index.css";

// Isolated calendar data. It exercises only the App presentation and never
// starts a workflow or calls an external publishing provider.
const workspaceId = "campaign-calendar-fixture";
const params = new URLSearchParams(location.search);
const calendarError = params.has("calendar-error");
const metricsPending = params.has("metrics-pending");
const narrow = params.has("narrow");
const demo = params.has("demo");
useConfigStore.setState({ loaded: true });

const calendarItems: WorkspaceAppDataItem[] = [
  {
    id: "morning-teaser", title: "Morning teaser", status: "published", calendar_at: "2026-10-22T01:00:00Z",
    fields: [
      { label: "Publish at", format: "date", value: "2026-10-22T01:00:00Z" },
      { label: "Platform", format: "provider", value: "Instagram", provider: "instagram", secondary: "@jr.music" },
      { label: "Publish by", format: "date", value: "2026-10-22T07:00:00Z" },
    ],
    published_at: "2026-10-22T01:03:00Z",
  },
  {
    id: "lyric-teaser", title: "Lyric teaser", status: "leased", calendar_at: "2026-10-22T04:30:00Z",
    execution: { run_id: "publish-lyric", status: "failed", current_step_id: "publish", error: "The publishing provider rejected this attempt.", href: "/flows/jr-music/runs/publish-lyric" },
    fields: [{ label: "Publish at", format: "date", value: "2026-10-22T04:30:00Z" }, { label: "Platform", format: "provider", value: "YouTube", provider: "youtube" }, { label: "Publish by", format: "date", value: "2026-10-22T10:30:00Z" }],
  },
  {
    id: "chorus-preview", title: "Chorus preview", status: "leased", calendar_at: "2026-10-22T07:00:00Z",
    fields: [{ label: "Publish at", format: "date", value: "2026-10-22T07:00:00Z" }, { label: "Platform", format: "provider", value: "Instagram", provider: "instagram", secondary: "@jr.music" }, { label: "Publish by", format: "date", value: "2026-10-22T13:00:00Z" }],
  },
  {
    id: "behind-the-song", title: "Behind the song", status: "preparation_blocked", calendar_at: "2026-10-22T10:00:00Z",
    detail: "Production needs attention.",
    fields: [{ label: "Publish at", format: "date", value: "2026-10-22T10:00:00Z" }, { label: "Platform", format: "provider", value: "TikTok", provider: "tiktok", secondary: "@jr.music" }, { label: "Publish by", format: "date", value: "2026-10-22T16:00:00Z" }, { label: "Preparation issue", value: "Production needs attention." }],
  },
  {
    id: "one-flag-raised", title: "One Flag, Raised", status: "ready", calendar_at: "2026-10-22T11:35:00Z",
    detail: "Preparation execution completed. Publication is still pending.",
    fields: [
      { label: "Publish at", format: "date", value: "2026-10-22T11:35:00Z" },
      { label: "Platform", format: "provider", value: "YouTube", provider: "youtube" },
      { label: "Publish by", format: "date", value: "2026-10-22T17:35:00Z" },
      { label: "Video", format: "document", href: "/viewer/01M631K8Z23VBSP41MY78AJ6AW" },
    ],
    execution: { run_id: "prepare-one-flag", status: "completed", current_step_id: "prepare", href: "/flows/jr-music/runs/prepare-one-flag" },
  },
  {
    id: "night-teaser", title: "Night teaser", status: "scheduled", calendar_at: "2026-10-22T13:00:00Z",
    fields: [{ label: "Publish at", format: "date", value: "2026-10-22T13:00:00Z" }, { label: "Platform", format: "provider", value: "TikTok", provider: "tiktok", secondary: "@jr.music" }, { label: "Publish by", format: "date", value: "2026-10-22T17:59:00Z" }],
  },
];

function exampleItems(rows: [string, string, string, string, string][]): WorkspaceAppDataItem[] {
  const labels: Record<string, string> = { instagram: "Instagram", youtube: "YouTube", tiktok: "TikTok" };
  return rows.map(([id, title, provider, calendar_at, status]) => ({
    id, title, status, calendar_at,
    ...(status === "published" ? { published_at: calendar_at } : {}),
    fields: [
      { label: "Publish at", format: "date", value: calendar_at },
      { label: "Platform", format: "provider", value: labels[provider] || provider, provider },
      { label: "Publish by", format: "date", value: new Date(Date.parse(calendar_at) + 6 * 60 * 60 * 1000).toISOString() },
    ],
  }));
}

const campaigns = [
  { id: "raise-that-flag", label: "Raise That Flag · v2", starts: "2026-10-22T00:00:00Z", ends: "2026-10-23T09:59:00Z", items: calendarItems },
  { id: "midnight-drive", label: "Midnight Drive · v1", starts: "2026-10-24T00:00:00Z", ends: "2026-11-02T15:59:00Z", items: exampleItems([
    ["release-clip", "Release clip", "youtube", "2026-10-24T10:00:00Z", "ready"],
    ["studio-session", "Studio session", "instagram", "2026-10-26T03:00:00Z", "ready"],
    ["midnight-lyric", "Midnight lyric teaser", "tiktok", "2026-10-28T11:30:00Z", "ready"],
    ["midnight-hook", "The midnight hook", "instagram", "2026-10-28T13:00:00Z", "ready"],
    ["midnight-visualizer", "Official visualizer", "youtube", "2026-10-30T10:00:00Z", "ready"],
    ["city-lights", "City lights preview", "tiktok", "2026-10-31T12:00:00Z", "scheduled"],
    ["listening-session", "Listening session", "instagram", "2026-11-01T03:00:00Z", "scheduled"],
    ["midnight-recap", "Release week recap", "youtube", "2026-11-02T09:00:00Z", "scheduled"],
  ]).concat([{
    // A finished post held for the owner: its run waits on Approve or Request changes.
    id: "studio-teaser", title: "Studio teaser", status: "awaiting_review", calendar_at: "2026-10-25T02:00:00Z",
    fields: [
      { label: "Publish at", format: "date", value: "2026-10-25T02:00:00Z" },
      { label: "Platform", format: "provider", value: "TikTok", provider: "tiktok", secondary: "@jr.music" },
      { label: "Video", format: "document", href: "/viewer/studio-teaser-video" },
    ],
    execution: { run_id: "review-studio-teaser", status: "paused", current_step_id: "review_prepared_content", href: "/flows/jr-music/runs/review-studio-teaser" },
  }]) },
  { id: "paper-planes", label: "Paper Planes · v1", starts: "2026-10-18T00:00:00Z", ends: "2026-10-21T09:59:00Z", items: exampleItems([
    ["paper-announcement", "Paper Planes is out", "instagram", "2026-10-18T01:00:00Z", "published"],
    ["paper-lyric", "Lyric video", "youtube", "2026-10-18T10:00:00Z", "published"],
    ["paper-chorus", "Sing the chorus", "tiktok", "2026-10-20T11:00:00Z", "published"],
    ["paper-thanks", "Thank you for listening", "instagram", "2026-10-21T03:00:00Z", "published"],
  ]) },
];

function campaignSummary(campaign: typeof campaigns[number]): WorkspaceAppDataContextSummary {
  const counts: Record<string, number> = {};
  for (const item of campaign.items) counts[item.status!] = (counts[item.status!] || 0) + 1;
  const next = campaign.items.find((item) => item.status !== "published");
  return {
    fields: [
      { label: "Campaign starts", format: "date" as const, value: campaign.starts },
      { label: "Campaign ends", format: "date" as const, value: campaign.ends },
      { label: "Collection planned until", format: "date" as const, value: campaign.ends },
      { label: "Reports scheduled until", format: "date" as const, value: "2026-11-08T09:59:00Z" },
    ],
    total: campaign.items.length, counts_by_stage: counts,
    next: next ? { ...next, label: next.title || next.id, fields: next.fields || [] } : undefined,
  };
}

const component: WorkspaceAppDataComponent = {
  type: "data_view" as const,
  schema_version: 1 as const,
  key: "campaign_results",
  title: "Campaign",
  description: null,
  context: { kind: "project" as const, source: "campaigns", label: "Campaign" },
  blocks: [
    {
      key: "plan_rollup", type: "project_rollup" as const, source: "slots", title: "Publishing progress", count_by_stage: true,
      completion: { stages: ["published"], label: "Published" },
      first: { stages: ["scheduled", "ready", "leased"] },
      stage_labels: { published: "Published", ready: "Ready to publish", scheduled: "Awaiting production", preparation_blocked: "Production needs attention" },
    },
    { key: "confirmed_plan", type: "documents" as const, source: "confirmed_plans", title: "Confirmed plan", limit: 1 },
    {
      key: "issues", type: "progress" as const, source: "issue_slots", title: "Action needed", limit: 5,
      stage_labels: { preparation_blocked: "Production needs attention", publication_blocked: "Publishing needs attention", verification_required: "Needs review" },
    },
    {
      key: "progress", type: "progress" as const, source: "pending_slots", title: "Upcoming publishing", limit: 5,
      stage_labels: { ready: "Ready to publish", scheduled: "Awaiting production", leased: "Processing", awaiting_review: "Waiting for your review" },
    },
    {
      key: "calendar_slots", type: "progress" as const, source: "slots", title: "Publishing calendar", limit: 100,
      stage_labels: { published: "Published", ready: "Ready to publish", scheduled: "Awaiting production", leased: "Processing", awaiting_review: "Waiting for your review", preparation_blocked: "Production needs attention", publication_blocked: "Publishing needs attention", verification_required: "Needs review" },
    },
    { key: "published", type: "records" as const, source: "publications", title: "Published content", metrics_source: "results" },
    { key: "tracking", type: "summary" as const, source: "campaigns", title: "Plan & reporting" },
    { key: "proposal", type: "documents" as const, source: "proposals", title: "Original proposal", limit: 1 },
    { key: "reports", type: "documents" as const, source: "reports", title: "Weekly reports", empty_message: "No weekly report yet. Reports will appear here when generated." },
    { key: "schedule", type: "progress" as const, source: "schedule", title: "Automatic schedule" },
  ],
  display: {
    layout: "stack",
    groups: [
      { key: "overview", title: "Publishing progress", blocks: ["plan_rollup", "confirmed_plan", "issues"] },
      { key: "timeline", title: "Upcoming publishing", blocks: ["progress", "calendar_slots"] },
      { key: "results", title: "Published content", blocks: ["published"] },
      { key: "reference", title: "Plan & reporting", blocks: ["tracking", "proposal", "reports", "schedule"] },
    ],
    hidden_blocks: ["schedule", "calendar_slots"],
    calendar: {
      block: "calendar_slots", date_field: ["due_at"], rollup_block: "plan_rollup", context_block: "tracking",
      attention_stages: ["awaiting_review", "preparation_blocked", "publication_blocked", "verification_required"], collection_title: "Campaigns",
    },
  },
};

api.workspaces.appViewContexts = async (_id, componentKey): Promise<WorkspaceAppDataContextsResponse> => {
  if (componentKey !== component.key) throw new Error("Unexpected component");
  return {
    component: { key: component.key, title: component.title, context: component.context },
    status: "ready",
    items: campaigns.map((campaign) => ({ kind: "project", id: campaign.id, label: campaign.label, status: "ready", created_at: campaign.starts, summary: campaignSummary(campaign) })),
  };
};

const reviewRun = {
  id: "review-studio-teaser", workspace_id: workspaceId, workflow_id: "jr-music", workflow_name: "Prepare and publish",
  status: "paused", current_step_id: "review_prepared_content", capabilities: { can_control: true },
  intervention: { kind: "workflow_approval", step_id: "review_prepared_content", options: ["approve", "request_changes"] },
  resume_action: {
    kind: "workflow_approval", step_id: "review_prepared_content", response_variable: "owner_review_decision",
    options: ["approve", "request_changes"], title: "Studio teaser", review_title: "Studio teaser",
    prompt: "Review this post before it is published.",
    review: { review_artifacts: [{ document_id: "studio-teaser-video", kind: "video", name: "Studio teaser.mp4" }] },
  },
};
let reviewAnswered = false;
api.workflows.getRun = async (runId) => {
  if (runId === reviewRun.id) return (reviewAnswered ? { ...reviewRun, status: "running", intervention: null, resume_action: null } : reviewRun) as any;
  if (runId !== "publish-lyric") throw new Error("Unexpected fixture run");
  return { id: runId, workspace_id: workspaceId, workflow_id: "jr-music", status: "failed", capabilities: { can_control: false } } as any;
};
// The answer goes to the run's own wait; the spec reads what was sent.
api.workflows.resumeRun = async (runId, variables, expectedStepId) => {
  (window as any).__reviewAnswers = [...((window as any).__reviewAnswers || []), { runId, variables, expectedStepId }];
  reviewAnswered = true;
  return { ...reviewRun, status: "running", intervention: null, resume_action: null } as any;
};

api.workspaces.appViewComponentData = async (_id, componentKey, options): Promise<WorkspaceAppDataResponse> => {
  const campaign = campaigns.find((item) => item.id === options.contextId);
  if (componentKey !== component.key || !campaign) throw new Error("Unexpected campaign request");
  const items = campaign.items;
  const summary = campaignSummary(campaign);
  const calendar: WorkspaceAppDataBlockResult = options.block === "calendar_slots" && options.cursor === "calendar-page-2"
    ? { key: "calendar_slots", type: "progress", source: "slots", title: "Publishing calendar", status: "ready", items: items.slice(3), next_cursor: null }
    : {
      key: "calendar_slots", type: "progress", source: "slots", title: "Publishing calendar", status: calendarError ? "error" : "ready",
      error: calendarError ? { message: "Publishing plan could not load." } : null,
      items: calendarError ? [] : items.slice(0, 3), next_cursor: calendarError || items.length <= 3 ? null : "calendar-page-2",
    };
  const blocks: WorkspaceAppDataBlockResult[] = [
    { key: "plan_rollup", type: "project_rollup", source: "slots", title: "Publishing progress", status: "ready", items: [{ id: "plan", total: summary.total, counts_by_stage: summary.counts_by_stage, next: summary.next ? { ...summary.next, stage: summary.next.status! } : undefined }] },
    { key: "confirmed_plan", type: "documents", source: "confirmed_plans", title: "Confirmed plan", status: "ready", items: [{ id: "confirmed-plan", document_id: "confirmed-plan", title: "Confirmed plan", viewer_url: "/viewer/confirmed-plan" }] },
    { key: "issues", type: "progress", source: "issue_slots", title: "Action needed", status: "ready", items: items.filter((item) => component.display!.calendar!.attention_stages.includes(item.status || "")) },
    { key: "progress", type: "progress", source: "pending_slots", title: "Upcoming publishing", status: "ready", items: items.filter((item) => ["ready", "scheduled", "leased"].includes(item.status || "")) },
    calendar,
    { key: "published", type: "records", source: "publications", title: "Published content", status: "ready", items: items.filter((item) => item.status === "published").map((item, index) => ({
      ...item, id: `${item.id}:0`, href: `https://example.com/posts/${item.id}`, metrics: [
        { id: `${item.id}-views`, metric_key: "views", label: "Views", value: campaign.id === "raise-that-flag" ? 0 : 12840 + index * 2450, unit: "count", value_type: "number", status: "fresh", updated_at: item.published_at },
        { id: `${item.id}-likes`, metric_key: "likes", label: "Likes", value: campaign.id === "raise-that-flag" ? (metricsPending ? null : 0) : 640 + index * 120, unit: "count", value_type: "number", status: campaign.id === "raise-that-flag" && metricsPending ? "no_data" : "fresh", updated_at: campaign.id === "raise-that-flag" && metricsPending ? null : item.published_at },
      ],
    })) },
    { key: "tracking", type: "summary", source: "campaigns", title: "Plan & reporting", status: "ready", items: [{ id: campaign.id, fields: summary.fields }] },
    { key: "proposal", type: "documents", source: "proposals", title: "Original proposal", status: "ready", items: [{ id: "original-plan", document_id: "original-plan", title: "Original proposal", viewer_url: "/viewer/original-plan" }] },
    { key: "reports", type: "documents", source: "reports", title: "Weekly reports", status: "empty", items: [] },
  ];
  // Match the project_records identity and cursor contract used by the API.
  const published = blocks.find((block) => block.key === "published")!;
  const laterResults = options.block === "published" && options.cursor === "published-page-2";
  if (published.items.length > 2) {
    published.items = laterResults ? published.items.slice(2) : published.items.slice(0, 2);
    published.next_cursor = laterResults ? null : "published-page-2";
  }
  return {
    component_key: component.key,
    context: { kind: "project", id: campaign.id },
    status: "ready",
    blocks: options.block ? blocks.filter((block) => block.key === options.block) : blocks,
  };
};

api.workspaces.appViewSection = async (_id, sectionId): Promise<WorkspaceAppSectionData> => {
  if (sectionId === "metrics") return {
    id: sectionId, block: "metrics", items: [
      "Campaign review score", "Distribution package score", "Handoff ready score", "Rights clearance score", "Royalty checklist score",
    ].map((title, index) => ({
      kind: "stat", id: `score-${index}`, title, value_type: "number", unit: null, value: null,
      updated_at: null, freshness_status: "no_data", collector_type: null, gaps: [],
      next_assessment_at: null, assessment: null, goal: null, series: [],
    })),
  };
  if (sectionId === "outputs") return {
    id: sectionId, block: "outputs", items: [{
      run_id: "campaign-plan", workflow_id: "launch", binding_id: "launch", title: "Launch a Music Marketing Campaign",
      result_title: "Raise That Flag campaign plan", result_summary: "Campaign plan confirmed and ready for production.",
      status: "completed", business_outcome: "completed", trigger_source: "manual",
      created_at: "2026-10-21T09:00:00Z", started_at: "2026-10-21T09:00:00Z", completed_at: "2026-10-21T09:05:00Z",
      files: [], file_count: 0,
    }],
  };
  throw new Error("Unexpected App section");
};

const view = {
  workspace_id: workspaceId,
  title: "JR Music Studio",
  description: "Promote your music, follow each campaign and track published content.",
  hero: "workflow:launch",
  config: { visual: "plain", layout: "feature", components: [component] },
  sections: ["actions", "metrics", "outputs"].map((block) => ({
    id: block, block, source: null, view: block === "outputs" ? "list" : "summary", limit: null,
    implicit: false, available: true, source_title: null,
  })),
  blocks: [
    { block: "actions", zone: "functions" }, { block: "metrics", zone: "data" }, { block: "outputs", zone: "outputs" },
  ],
  actions: [{ key: "workflow:launch", kind: "workflow", binding_id: "launch", workflow_id: "launch", title: "Launch a Music Marketing Campaign", description: "Create a campaign", inputs: [] }],
};

function Fixture() {
  return <main style={{ maxWidth: narrow ? 390 : 1180, margin: "0 auto", padding: 24 }}>
    {demo && <aside aria-label="Demo controls" style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "10px 20px", padding: "12px 0 20px", color: "var(--text-muted)", fontSize: 13 }}>
      <span>Campaign 样式预览 · 演示数据</span>
      <a href="?demo">桌面</a><a href="?demo&narrow">移动端 390px</a><a href="?demo&calendar-error">加载异常</a>
      <span style={{ flexBasis: "100%", fontSize: 12 }}>点击下方 Campaign 查看：10 月 22 日六条计划、跨月排期、已发布内容和指标。仅用于展示，文档和运行链接为占位，不会启动真实工作流。</span>
    </aside>}
    <WorkspaceAppView view={view as any} loading={false} error={false} busy={false} canStart
      onRetry={() => {}} onAction={async () => ({ status: "completed" } as any)} onEdit={() => {}} onClose={() => {}} />
  </main>;
}

createRoot(document.getElementById("root")!).render(
  <MemoryRouter><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <Fixture />
  </QueryClientProvider></MemoryRouter>,
);
