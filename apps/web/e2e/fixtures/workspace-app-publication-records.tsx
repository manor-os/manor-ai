import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider, useIsFetching } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

import { api } from "../../src/lib/api";
import WorkspaceAppView from "../../src/components/workspaces/WorkspaceAppView";
import "../../src/index.css";

const workspaceId = "publication-records-fixture";
const params = new URLSearchParams(location.search);
const projectsOnly = params.has("projects");
const summary = params.has("summary");
const crossYear = params.has("cross-year");
const inspection = params.has("inspection");
const schedule = params.has("schedule");
const training = params.has("training");
const trainingEmpty = training && params.has("empty-schedule");
const deliveryScenario = schedule || training;
const longRunError = params.has("long-run-error");
const noRollupFirst = params.has("no-rollup-first");
const singleRollup = params.has("single-rollup");
const singleRollupMode = params.get("single-rollup") || "complete";
const singleRollupWithFirst = params.has("single-rollup-next");
const rollupEmpty = params.has("rollup-empty");
const stackGroups = params.has("stack-groups");
const blockState = params.get("block");
let tracking = params.get("tracking");
const recovery = params.get("recovery");
let staleRetryAuthorization = false;
let retryRequests = 0;
let runtimeApprovalRequests = 0;
let tiktokReviewRefreshes = 0;
let lastRuntimeApproval: Record<string, unknown> | null = null;
const recoveryRetryAction = {
  kind: "workflow_retry",
  step_id: "prepare_lesson",
  retry_from_step_id: "prepare_lesson",
  options: ["retry"],
  observed_problem: "The delivery worker stopped before preparing the lesson.",
  preserved_receipts: [],
  editable_input_schema: { type: "object", properties: {} },
};
const safeRecoveryRun = {
  id: "course-recovery-safe",
  workspace_id: workspaceId,
  workflow_id: "lesson-flow",
  binding_id: "lesson-delivery",
  workflow_name: "Lesson delivery",
  status: "failed",
  current_step_id: "prepare_lesson",
  current_step_name: "Prepare lesson",
  error: "The delivery worker stopped before preparing the lesson.",
  capabilities: { can_control: true },
  retry_from_step_id: "prepare_lesson",
  intervention: recoveryRetryAction,
};
const cancelledRecoveryRun = {
  ...safeRecoveryRun,
  id: "course-recovery-cancelled",
  status: "cancelled",
  error: "The delivery was cancelled before the external action began.",
};
const reviewRecoveryRun = {
  ...safeRecoveryRun,
  id: "course-recovery-review",
  error: "The external delivery result must be reviewed before another attempt.",
  retry_from_step_id: undefined,
  intervention: null,
};
const readonlyRecoveryRun = {
  ...safeRecoveryRun,
  id: "course-recovery-readonly",
  capabilities: { can_control: false },
  intervention: null,
};
const staleRecoveryRun = {
  ...safeRecoveryRun,
  capabilities: { can_control: false },
  retry_from_step_id: undefined,
  intervention: null,
};
const tiktokReview = {
  kind: "tiktok_publish", version: 2,
  review_id: "workspace-app-tiktok-review",
  creator_info: {
    creator_nickname: "Workspace fixture creator",
    privacy_level_options: ["PUBLIC_TO_EVERYONE"],
    comment_disabled: false, duet_disabled: false, stitch_disabled: false,
    max_video_post_duration_sec: 60,
  },
  media: { type: "photo", items: [
    { type: "image", name: "workspace-app-cover.gif", url: "https://tiktok.fixture/workspace-app-cover.gif" },
  ] },
  initial_caption: "Original Workspace App caption",
};
const tiktokRecoveryRun = {
  ...safeRecoveryRun,
  id: "course-recovery-tiktok",
  status: "paused",
  current_step_id: "publish_to_tiktok",
  intervention: null,
  resume_action: {
    kind: "workflow_approval", step_id: "publish_to_tiktok", workflow_run_id: "course-recovery-tiktok",
    runtime_approval_id: "workspace-app-native-tiktok", options: ["approve", "reject"],
    tool: "mcp__tiktok__post_photo", review: tiktokReview,
  },
};
const ordinaryPausedRecoveryRun = {
  ...safeRecoveryRun,
  id: "course-recovery-ordinary-paused",
  status: "paused",
  current_step_id: "review_copy",
  intervention: null,
  resume_action: {
    kind: "workflow_approval", step_id: "review_copy", workflow_run_id: "course-recovery-ordinary-paused",
    runtime_approval_id: "workspace-app-native-ordinary", options: ["approve", "reject"],
    review: { title: "Review the lesson copy" },
  },
};
const readonlyPausedRecoveryRun = {
  ...ordinaryPausedRecoveryRun,
  id: "course-recovery-paused-readonly",
  capabilities: { can_control: false },
};
const backgroundWaitRecoveryRun = {
  ...ordinaryPausedRecoveryRun,
  id: "course-recovery-background-wait",
  resume_action: null,
};
const unattendedPausedRun = {
  id: "run-review",
  workspace_id: workspaceId,
  status: "paused",
  capabilities: { can_control: false },
  resume_action: null,
};
let recoveryCurrent: any = recovery === "tiktok" ? tiktokRecoveryRun
  : recovery === "ordinary-paused" ? ordinaryPausedRecoveryRun
  : recovery === "paused-readonly" ? readonlyPausedRecoveryRun
  : recovery === "background-wait" ? backgroundWaitRecoveryRun
  : recovery === "reconcile" ? { ...reviewRecoveryRun, intervention: {
  kind: "workflow_effect_review", step_id: "prepare_lesson", effect_fingerprint: "a".repeat(64), options: ["review"],
  deadline_at: "2026-10-15T08:00:00Z",
} } : recovery === "blocked" ? { ...reviewRecoveryRun, intervention: {
  kind: "workflow_effect_review", options: [], blocked_reason: "external_effect_requires_reconciliation",
} } : recovery === "review" ? reviewRecoveryRun
  : recovery === "readonly" ? readonlyRecoveryRun
    : recovery === "cancelled" ? cancelledRecoveryRun
    : safeRecoveryRun;
function recoveryExecution() {
  if (!recovery) return null;
  return {
    run_id: recoveryCurrent.id,
    status: recoveryCurrent.status,
    current_step_id: recoveryCurrent.current_step_id,
    error: recoveryCurrent.error,
    href: `/flows/${recoveryCurrent.workflow_id}/runs/${recoveryCurrent.id}`,
  };
}
const component = {
  type: "data_view" as const,
  schema_version: 1 as const,
  key: "publication_data",
  title: inspection ? "Delivery review" : training ? "Training delivery" : schedule ? "Lesson schedule" : summary ? "Course" : "Publication tracking",
  context: inspection || projectsOnly || tracking || summary || deliveryScenario ? { kind: "project" as const, source: inspection ? "deliveries" : "courses", label: inspection ? "Delivery" : training ? "Training plan" : "Course" } : { kind: "workspace" as const, label: "Workspace" },
  ...(tracking ? { visibility: { block: "published", when: (tracking === "records" ? "has_records" : "has_observations") as const } } : {}),
  blocks: [...(inspection ? [{
    key: "deliveries", type: "records" as const, source: "delivery_records", title: "Delivered work",
    record_display: { media: [{ source: "delivery_images", label: "Preview" }] },
  }] : deliveryScenario ? [{
    key: "lesson_rollup", type: "project_rollup" as const, source: "lessons", title: "Lesson plan",
    count_by_stage: true,
    ...(training ? {
      title: "Training delivery progress",
      completion: { stages: ["delivered"], label: "Delivered" },
      stage_labels: { delivered: "Delivered", awaiting_learner_feedback: "Awaiting learner feedback" },
      empty_message: "No training delivery schedule yet.",
    } : noRollupFirst || (singleRollup && !singleRollupWithFirst) ? {} : { first: { stages: ["verification_required", "scheduled", "ready"] } }),
    ...(rollupEmpty ? { empty_message: "No lesson schedule yet." } : {}),
  }, {
    key: "lesson_progress", type: "progress" as const, source: "lessons", title: training ? "Upcoming training delivery" : "Lesson execution",
    ...(training ? { stage_labels: { delivered: "Delivered", awaiting_learner_feedback: "Awaiting learner feedback" } } : {}),
  }, {
    key: "reminders", type: "progress" as const, source: "reminders", title: "Reminder checks",
  }] : summary ? [{
    key: "campaign_context", type: "summary" as const, source: "courses", title: "Campaign context",
    fields: [
      { label: "Release status", field: ["release", "status"], format: "status" as const },
      { label: "Release date", field: ["release", "date"], format: "date" as const },
      { label: "Course starts", field: ["schedule", "start"], format: "date" as const },
      { label: "Course ends", field: ["schedule", "end"], format: "date" as const },
      { label: "Listen", field: ["release", "links"], format: "link_list" as const },
    ],
  }] : []), ...(!inspection && !deliveryScenario ? [{
    key: "published",
    type: "records" as const,
    source: "publications",
    metrics_source: "results",
    title: "Published content",
    record_display: {
      title: { source: "slots", field: ["production", "publication", "title"] },
      fields: [{ label: "Work", source: "campaigns", field: ["work", "title"] }],
    },
  }] : []), ...(!inspection && !projectsOnly ? [
    { key: "reports", type: "documents" as const, source: "reports", title: "Weekly reports" },
    { key: "notes", type: "records" as const, source: "notes", title: "Course materials" },
    { key: "archive", type: "documents" as const, source: "archive", title: "Archived drafts" },
  ] : [])],
  ...(stackGroups ? { display: { layout: "stack" as const, groups: [
    { key: "publication", title: "Publication data", blocks: ["published"] },
    { key: "materials", title: "Course materials", blocks: ["notes"] },
    { key: "reports", title: "Weekly reports", blocks: ["reports", "archive"] },
  ] } } : {}),
};

api.workspaces.appViewContexts = async () => {
  if (tracking === "error") throw new Error("Tracking source unavailable");
  return ({ component: { key: component.key, context: component.context }, items: tracking === "empty" ? [] : tracking ? [
  { kind: "project", id: inspection ? "delivery-ready" : "course-ready", label: inspection ? "Kitchen renovation" : "Watercolor basics" },
] : inspection ? [{ kind: "project", id: "delivery-ready", label: "Kitchen renovation" }] : projectsOnly || summary ? [
  { kind: "project", id: "course-new", label: "Watercolor basics · Second edition", created_at: "2026-10-13T11:35:00Z" },
  { kind: "project", id: "course-old", label: "Watercolor basics · Second edition", created_at: "2026-10-12T11:28:00Z" },
] : deliveryScenario ? [{ kind: "project", id: "course-ready", label: training ? "Customer onboarding" : "Watercolor basics" }] : [{ kind: "workspace", id: workspaceId, label: "Publication studio" }], status: "ready" } as any);
};
api.workspaces.appViewComponentData = async () => ({
  component_key: component.key,
  context: { kind: "workspace", id: workspaceId },
  status: "ready",
  ...(tracking ? { visible: tracking !== "revoked" } : {}),
  blocks: [...(inspection ? [{
    key: "deliveries", type: "records", source: "delivery_records", title: "Delivered work", status: "ready", items: [{
      id: "inspection-delivery", title: "Kitchen cabinet inspection", href: "/records/inspection-delivery", status: "ready_for_review",
      media: [
        { document_id: "inspection-image", title: "Kitchen cabinet", mime_type: "image/jpeg", viewer_url: "/viewer/inspection-image", label: "Preview" },
        { document_id: "inspection-video", title: "Kitchen walkthrough", mime_type: "video/mp4", viewer_url: "/viewer/inspection-video", label: "Video original" },
      ],
    }],
  }] : deliveryScenario ? [{
    key: "lesson_rollup", type: "project_rollup", source: "lessons", title: training ? "Training delivery progress" : "Lesson plan", status: trainingEmpty ? "empty" : "ready", items: trainingEmpty ? [] : [{
      id: "rollup:lessons", total: training ? trainingEmpty ? 0 : 3 : singleRollup ? singleRollupMode === "zero" ? 0 : 2 : 4, counts_by_stage: training ? trainingEmpty ? {} : { delivered: 2, awaiting_learner_feedback: 1 } : singleRollup ? { published: singleRollupMode === "zero" ? 0 : singleRollupMode === "partial" ? 1 : 2 } : { scheduled: 2, ready: 1, verification_required: 1 }, next: {
        id: "lesson-verify", label: training ? "Module three feedback" : "Lesson three review", stage: training ? "awaiting_learner_feedback" : "verification_required", fields: [{ label: "Due", format: "date", value: "2026-10-15T09:00:00Z" }, { label: "Notes", value: "The reviewer needs the source footage, approved copy, release timing, audience notes, and the final account selection before the campaign can continue." }],
        execution: { run_id: "run-initialize", status: "completed", current_step_id: "initialize", href: "/flows/lesson-flow/runs/run-initialize" },
      },
    }],
  }, {
    key: "lesson_progress", type: "progress", source: "lessons", title: training ? "Upcoming training delivery" : "Lesson execution", status: "ready", items: [{
      id: "lesson-verify", label: training ? "Module three feedback" : "Lesson three review", status: training ? "awaiting_learner_feedback" : "verification_required", detail: training ? "Learner feedback is ready for review" : "Video proof is ready for review", fields: [{ label: "Due", format: "date", value: "2026-10-15T09:00:00Z" }, { label: "Notes", value: "The reviewer needs the source footage, approved copy, release timing, audience notes, and the final account selection before the campaign can continue." }],
      execution: recoveryExecution() || { run_id: "run-review", status: "paused", current_step_id: "review_media", error: longRunError ? "The reviewer needs the source footage, approved copy, release timing, audience notes, final account selection, campaign permissions, disclosure confirmation, and tracking identifiers before this delivery can safely continue. Confirm each requirement, correct the unresolved record, and resume the existing run instead of creating a second delivery attempt." : "Needs a reviewer.", href: "/flows/lesson-flow/runs/run-review" },
    }, ...(!training ? [{
      id: "lesson-scheduled", label: "Lesson four", status: "scheduled", fields: [{ label: "Due", format: "date", value: "2026-10-16T09:00:00Z" }, { label: "Platform", format: "provider", value: "TikTok", provider: "tiktok", secondary: "@courses" }],
      execution: { run_id: "run-finished", status: "completed", current_step_id: "initialize", href: "/flows/lesson-flow/runs/run-finished" },
    }] : [])],
  }, {
    key: "reminders", type: "progress", source: "reminders", title: "Reminder checks", status: "ready", items: [{
      id: "course-reminder-disabled", label: "Disabled reminder", status: "disabled", enabled: false, state: "disabled", last_status: "completed", last_run_at: "2026-10-14T08:00:00Z", next_run_at: null, stop_after: "2026-10-16T12:00:00Z",
    }, {
      id: "course-reminder", label: "Course reminder", status: "ended", enabled: false, state: "ended", last_status: "completed", last_run_at: "2026-10-14T08:00:00Z", next_run_at: null, stop_after: "2026-10-14T12:00:00Z",
    }],
  }] : summary ? [{
    key: "campaign_context", type: "summary", source: "courses", title: "Campaign context", status: blockState || "ready", items: blockState ? [] : [{
      id: "course-summary", fields: [
        { label: "Release status", format: "status", value: "Unreleased" },
        { label: "Release date", format: "date", value: "2026-11-03" },
        { label: "Course starts", format: "date", value: "2026-11-03T09:00:00Z" },
        { label: "Course ends", format: "date", value: crossYear ? "2027-01-17" : "2026-11-17T17:00:00Z" },
        { label: "Blocker", format: "text", value: "The selected account still needs a verified connection before the campaign can publish. Review the account identity, confirm the access scope, and resume the approved schedule after the connection is available." },
        { label: "Listen", format: "link_list", value: null, links: [
          { label: "Artist site", href: "https://example.com/listen" },
          { label: "Malformed URL", href: "https://" },
        ] },
      ],
    }],
  }] : []), ...(!inspection && !deliveryScenario ? [{
    key: "published", type: "records", source: "publications", title: "Published content", status: blockState || "ready",
    ...(blockState === "error" ? { error: { message: "Publication data unavailable." } } : {}),
    items: projectsOnly || blockState === "error" ? [] : [
      {
        id: "lesson-zero", title: "Lesson one", subtitle: "youtube", platform: "youtube", href: "https://www.youtube.com/watch?v=courseintro", status: "published", tracking_status: "configured",
        published_at: "2026-10-10T10:00:00Z", fields: [{ label: "Work", value: "Course introduction" }, { label: "Edition", value: "revised_course" }, { label: "Notes", value: "This release includes the approved trailer, campaign copy, destination links, channel direction, and a reminder to review early audience feedback before the next scheduled post." }],
        media: [{ document_id: "lesson-video", title: "Course trailer", mime_type: "video/mp4", viewer_url: "/viewer/lesson-video", label: "Video original" }],
        execution: { run_id: "run-publication-one", status: "completed", current_step_id: "persist_receipt", href: "/flows/lesson-flow/runs/run-publication-one" },
        metrics: ["views", "likes", "comments"].map((key, index) => ({
          id: `lesson-zero-${key}`, metric_key: key, value: [0, 12, 4][index], unit: "count", value_type: "number", status: "stale", freshness: "stale",
          updated_at: `2026-10-11T10:0${index}:00Z`, ...(index === 1 ? { change: 3 } : { delta_reason: "missing_comparable_baseline" }),
          trend: [{ value: null, observed_at: `2026-10-10T10:0${index}:00Z` }, { value: [0, 12, 4][index], observed_at: `2026-10-11T10:0${index}:00Z` }],
        })),
      },
      {
        id: "lesson-missing", title: "Lesson two", subtitle: "instagram", platform: "instagram", href: "https://www.instagram.com/reel/courseoutro/", status: "published", tracking_status: "partial",
        published_at: "2026-10-12T10:00:00Z", fields: [{ label: "Work", value: "Course conclusion" }, { label: "Account", format: "provider", value: "Instagram", provider: "instagram", secondary: "@course.studio" }],
        conflict: "The receipt references two external IDs.",
        metrics: [{ id: "lesson-missing-likes", metric_key: "likes", label: "Likes", value: null, unit: "count", value_type: "number", status: "no_data", collection_error: "Likes are unavailable.", updated_at: "2026-10-12T10:00:00Z" }],
      },
      {
        id: "lesson-preview", title: "Course preview", subtitle: "tiktok", platform: "tiktok", href: "https://www.tiktok.com/@courses/video/1234567890123456789", status: "published", tracking_status: "pending",
        published_at: "2026-10-12T11:00:00Z",
      },
    ],
  }] : []), ...(!inspection && !projectsOnly && !schedule ? [{
    key: "reports", type: "documents", source: "reports", title: "Weekly reports", status: "ready",
    items: [{ id: "report-document", document_id: "report-document", title: "weekly-report-01M4TTSDVDZ670M0KKVXJRHJ4J.md", updated_at: "2026-10-12T10:00:00Z" }],
  }, {
    key: "notes", type: "records", source: "notes", title: "Course materials", status: "ready",
    items: [{ id: "course-guide", title: "Painting guide", subtitle: "Learning portal" }],
  }, {
    key: "archive", type: "documents", source: "archive", title: "Archived drafts", status: "ready", items: [],
  }] : [])],
} as any);

if (inspection) {
  const thumbnail = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='32' height='24'%3E%3Crect width='32' height='24' fill='%235c8d77'/%3E%3C/svg%3E";
  api.documents.imageThumbnail = async () => thumbnail;
  api.documents.thumbnail = async () => thumbnail;
}

// The App may show the compact status, but any paused or retryable run is
// re-read before it exposes a control.
api.workflows.getRun = async (runId, detail = true) => {
  if (!recovery && runId === unattendedPausedRun.id) return unattendedPausedRun;
  if (recovery && runId === recoveryCurrent.id) {
    const current = recovery === "stale" && staleRetryAuthorization ? staleRecoveryRun : recoveryCurrent;
    // Compact run responses expose pending requests through intervention only;
    // the review dialog must fetch detail before it can submit resume_action.
    return {
      ...current,
      intervention: current.resume_action || current.intervention,
      resume_action: detail ? current.resume_action : null,
    };
  }
  throw new Error("Unexpected recovery run");
};

if (recovery) {
  api.chat.refreshTikTokPublishReview = async (approvalId) => {
    if (approvalId !== "workspace-app-native-tiktok") throw new Error("Unexpected TikTok approval");
    tiktokReviewRefreshes += 1;
    window.dispatchEvent(new Event("fixture-runtime-approval-request"));
    return tiktokReview;
  };
  api.workflows.resolveRuntimeApproval = async (runId, approvalId, choice, review?: unknown) => {
    runtimeApprovalRequests += 1;
    lastRuntimeApproval = { run_id: runId, approval_id: approvalId, choice, review };
    window.dispatchEvent(new Event("fixture-runtime-approval-request"));
    if (recovery !== "tiktok" || runId !== tiktokRecoveryRun.id || approvalId !== "workspace-app-native-tiktok"
      || choice !== "approve" || !review || runtimeApprovalRequests === 1) {
      throw new Error("The first Workspace App approval attempt was rejected.");
    }
    recoveryCurrent = { ...tiktokRecoveryRun, status: "running", resume_action: null };
    return recoveryCurrent;
  };
  api.workflows.retryRun = async (runId, payload) => {
    retryRequests += 1;
    window.dispatchEvent(new Event("fixture-retry-request"));
    const reviewed = recovery === "reconcile" && runId === reviewRecoveryRun.id
      && payload.effect_review?.confirmed === true && payload.effect_review?.decision === "not_applied"
      && payload.effect_review?.effect_fingerprint === "a".repeat(64) && payload.effect_review?.note.length >= 20;
    const ordinaryRecovery = (recovery === "safe" && runId === safeRecoveryRun.id)
      || (recovery === "cancelled" && runId === cancelledRecoveryRun.id);
    if ((!reviewed && !ordinaryRecovery) || payload.from_step_id !== "prepare_lesson") {
      throw new Error("Retry request must be rejected after recovery authorization changes.");
    }
    recoveryCurrent = {
      ...safeRecoveryRun,
      id: "course-recovery-running",
      status: "running",
      error: null,
      intervention: null,
      retry_of_run_id: safeRecoveryRun.id,
      attempt_number: 2,
    };
    return recoveryCurrent;
  };
}

const view = {
  workspace_id: workspaceId,
  title: "Publication studio",
  description: "Track verified releases.",
  hero: "workflow:publish",
  config: { visual: "plain", layout: "feature", components: [component] },
  actions: [{ key: "workflow:publish", kind: "workflow", binding_id: "publish", workflow_id: "publish", title: "Publish content", description: "Start a new publication", inputs: [] }],
};

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
function Fixture() {
  const fetching = useIsFetching();
  const [retryAttemptCount, setRetryAttemptCount] = useState(retryRequests);
  const [runtimeApprovalAttemptCount, setRuntimeApprovalAttemptCount] = useState(runtimeApprovalRequests);
  const [tiktokReviewRefreshCount, setTikTokReviewRefreshCount] = useState(tiktokReviewRefreshes);
  useEffect(() => {
    const refreshRetryAttemptCount = () => setRetryAttemptCount(retryRequests);
    const refreshRuntimeApprovalAttemptCount = () => {
      setRuntimeApprovalAttemptCount(runtimeApprovalRequests);
      setTikTokReviewRefreshCount(tiktokReviewRefreshes);
    };
    window.addEventListener("fixture-retry-request", refreshRetryAttemptCount);
    window.addEventListener("fixture-runtime-approval-request", refreshRuntimeApprovalAttemptCount);
    return () => {
      window.removeEventListener("fixture-retry-request", refreshRetryAttemptCount);
      window.removeEventListener("fixture-runtime-approval-request", refreshRuntimeApprovalAttemptCount);
    };
  }, []);
  return <>
    {tracking && <aside>
      <output data-testid="fixture-query-state">{fetching ? "Loading" : "Ready"}</output>
      <button onClick={() => { tracking = "ready"; void queryClient.invalidateQueries(); }}>Collect publication data</button>
    </aside>}
    {recovery && <aside>
      {recovery === "stale" && <button onClick={() => { staleRetryAuthorization = true; }}>Expire retry authorization</button>}
      <output data-testid="fixture-retry-requests">Retry requests: {retryAttemptCount}</output>
      <output data-testid="fixture-runtime-approval-requests">Runtime approval requests: {runtimeApprovalAttemptCount}</output>
      <output data-testid="fixture-runtime-approval-payload">{JSON.stringify(lastRuntimeApproval)}</output>
      <output data-testid="fixture-tiktok-review-refreshes">TikTok review refreshes: {tiktokReviewRefreshCount}</output>
    </aside>}
    <WorkspaceAppView view={view as any} loading={false} error={false} busy={false} canStart onRetry={() => {}} onAction={async () => ({ status: "completed" } as any)} onEdit={() => {}} onClose={() => {}} />
  </>;
}

createRoot(document.getElementById("root")!).render(
  <MemoryRouter><QueryClientProvider client={queryClient}>
    <Fixture />
  </QueryClientProvider></MemoryRouter>,
);
