import { createContext, useContext, useEffect, useId, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent, type ReactNode } from "react";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";

import { api, type WorkspaceAppDataBlockResult, type WorkspaceAppDataComponent, type WorkspaceAppDataContext, type WorkspaceAppDataItem, type WorkspaceAppDataMetric } from "../../lib/api";
import { formatDateCompact, formatDateLong, relativeTime } from "../../lib/format";
import { t } from "../../lib/i18n";
import { formatWorkspaceStatValue, workspaceStatAttentionPresentation } from "../../lib/workspaceStats";
import { IconArrowRight, IconDocument, IconDownload, IconFolder, IconMicrophone, IconMusicNote, IconPlay, IconRefresh } from "../icons";
import IntegrationLogo from "../IntegrationLogo";
import Button from "../ui/Button";
import CompactCard from "../ui/CompactCard";
import InlineDocumentText, { inlineDocumentTextKind } from "../InlineDocumentText";
import WorkflowRunRetryControl from "../workflows/WorkflowRunRetryControl";
import WorkflowRunResumeControl from "../workflows/WorkflowRunResumeControl";
import { ReviewPlayer, ReviewText } from "../workflows/ReviewDeliverables";
import { artifactKind, type ReviewArtifactReference } from "../workflows/WorkflowApprovalReview";
import { workflowRetrySchemaIsCompatible, type WorkflowHistoryRun } from "../workflows/workflowRunDisplay";
import WorkspaceAppCalendarView from "./WorkspaceAppCalendarView";
import MakingMark, { MakingCover } from "./WorkspaceAppMakingMark";
import { closeDetail, openDetail } from "../../stores/detail";
import { runStatusLabel, titleHues } from "./workspaceAppRunDisplay";
import OneLineText from "../ui/OneLineText";
import "./WorkspaceAppDataView.css";

/** Opens a run where its progress shows: its App entry, or the run page. */
export type WorkspaceAppRunOpener = (run: { id: string; workflow_id: string; binding_id?: string | null }) => void;
/** Opens one of the App's own actions with some of its inputs already filled (a pack's Remake). */
export type WorkspaceAppActionOpener = { has: (actionKey: string) => boolean; open: (actionKey: string, values: Record<string, string>) => void };
const RunOpenerContext = createContext<WorkspaceAppRunOpener | null>(null);
const ActionOpenerContext = createContext<WorkspaceAppActionOpener | null>(null);

type Props = { workspaceId: string; component: WorkspaceAppDataComponent; preview?: boolean; openRun?: WorkspaceAppRunOpener; openAction?: WorkspaceAppActionOpener; onFocusChange?: (componentKey: string, focused: boolean) => void };
const EMPTY_CONTEXTS: WorkspaceAppDataContext[] = [];
const PLATFORM_LABELS = new Map([["youtube", "YouTube"], ["instagram", "Instagram"], ["tiktok", "TikTok"]]);
const SUMMARY_PREVIEW_LENGTH = 120;
function isLongText(value?: string | null): value is string {
  return Boolean(value && (value.length > SUMMARY_PREVIEW_LENGTH || value.includes("\n")));
}
function humanize(value?: string | null): string {
  const text = String(value || "").trim();
  return /^[a-z0-9]+(?:[_-][a-z0-9]+)+$/.test(text) ? text.replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase()) : text;
}
function stateLabel(value?: string | null): string {
  const text = humanize(value);
  return /^[a-z]+$/.test(text) ? `${text.slice(0, 1).toUpperCase()}${text.slice(1)}` : text;
}
function platformLabel(value?: string | null): string {
  return PLATFORM_LABELS.get(String(value || "").trim().toLowerCase()) || humanize(value);
}
function PlatformLabel({ value }: { value: string }) {
  const key = value.trim().toLowerCase();
  return <span className="workspace-app-data__platform">
    {PLATFORM_LABELS.has(key) && <IntegrationLogo provider={key} size={15} />}
    {platformLabel(value)}
  </span>;
}
function ProviderValue({ provider, label, secondary }: { provider?: string | null; label?: string | null; secondary?: string | null }) {
  const name = label || platformLabel(provider);
  return <span className="workspace-app-data__platform" title={secondary ? `${name} · ${secondary}` : name}>
    {provider && <IntegrationLogo provider={provider} size={15} />}
    {name || "—"}
    {secondary && <small className="workspace-app-data__platform-account">{secondary}</small>}
  </span>;
}
function metricLabel(item: WorkspaceAppDataMetric): string {
  const key = humanize(item.metric_key);
  return key ? key.replace(/^\w/, (letter) => letter.toUpperCase()) : item.label || item.title || t("component.workspace_app_view.data_metric");
}
function metricValue(item: WorkspaceAppDataMetric): string {
  if (item.value === null || item.value === undefined || item.value === "") return "—";
  if (typeof item.value === "string") return item.value;
  if (item.unit?.toLowerCase() === "count") return item.value.toLocaleString();
  return formatWorkspaceStatValue({ current_value: item.value, value_type: item.value_type || "number", unit: item.unit || null, freshness_status: item.status || "no_data" });
}
function isSafeHref(href: string): boolean { return /^\/(?![\/\\])/.test(href) || /^https:\/\//i.test(href); }
function isSafePublicHref(href: string): boolean {
  try {
    const url = new URL(href);
    return (url.protocol === "http:" || url.protocol === "https:") && Boolean(url.hostname);
  } catch { return false; }
}
function publicUrlHostname(value?: string | null): string | null {
  if (!value || !/^https:\/\//i.test(value) || !isSafeHref(value)) return null;
  try { return new URL(value).hostname || null; } catch { return null; }
}
function sameDisplayLabel(left?: string | null, right?: string | null): boolean {
  const normalizedLeft = humanize(left).toLocaleLowerCase();
  return Boolean(normalizedLeft && normalizedLeft === humanize(right).toLocaleLowerCase());
}
function safeHref(item: WorkspaceAppDataItem): string | null {
  const href = item.viewer_url || item.href || (item.document_id ? `/viewer/${encodeURIComponent(item.document_id)}` : null);
  return href && isSafeHref(href) ? href : null;
}
function safeContextHref(context: WorkspaceAppDataContext | null): string | null { return context?.href && isSafeHref(context.href) ? context.href : null; }
function statusClass(status?: string | null): string {
  if (["completed", "ready", "fresh", "published", "success"].includes(String(status).toLowerCase())) return "is-ready";
  if (["failed", "error", "blocked", "cancelled"].includes(String(status).toLowerCase())) return "is-error";
  return "";
}

function ItemLink({ item, children, className = "" }: { item: WorkspaceAppDataItem; children: ReactNode; className?: string }) {
  const href = safeHref(item); const classes = `workspace-app-data__item ${className}`.trim();
  if (!href) return <div className={classes}>{children}</div>;
  const external = /^https:\/\//i.test(href);
  return <a className={classes} href={href} {...(external ? { target: "_blank", rel: "noreferrer" } : {})}>{children}<IconArrowRight size={15} aria-hidden="true" /></a>;
}

function RecordMedia({ media }: { media: NonNullable<WorkspaceAppDataItem["media"]> }) {
  return <div className={`workspace-app-data__record-media${media.length === 1 ? " is-single" : ""}`} aria-label={t("component.workspace_app_view.data_media")}>
    {media.slice(0, 4).map((item) => <RecordMediaItem key={item.document_id} item={item} />)}
  </div>;
}

function RecordMediaItem({ item }: { item: NonNullable<WorkspaceAppDataItem["media"]>[number] }) {
  const [thumbnail, setThumbnail] = useState("");
  const [failed, setFailed] = useState(false);
  const href = item.viewer_url && isSafeHref(item.viewer_url) ? item.viewer_url : null;
  const label = item.label || item.title || t("component.workspace_app_view.data_document");
  useEffect(() => {
    let active = true;
    let objectUrl = "";
    setThumbnail("");
    setFailed(false);
    const load = async () => {
      try {
        objectUrl = await api.documents.thumbnail(item.document_id, {
          cache: true,
          version: `${item.updated_at || ""}:${item.file_size || ""}`,
        });
        if (active) setThumbnail(objectUrl);
        else if (objectUrl.startsWith("blob:")) URL.revokeObjectURL(objectUrl);
      } catch {
        if (active) setFailed(true);
      }
    };
    void load();
    return () => {
      active = false;
      if (objectUrl.startsWith("blob:")) URL.revokeObjectURL(objectUrl);
    };
  }, [item.document_id, item.file_size, item.updated_at]);

  const content = !thumbnail || failed
    ? <span className="workspace-app-data__record-media-fallback" aria-busy={!failed}>
      <IconDocument size={20} aria-hidden="true" />
      {!failed && <small>{t("component.workspace_app_view.data_media_loading")}</small>}
    </span>
    : <img src={thumbnail} alt={label} loading="lazy" onError={() => setFailed(true)} />;
  return href
    ? <a className="workspace-app-data__record-media-item" href={href} aria-label={label}>{content}</a>
    : <span className="workspace-app-data__record-media-item" aria-label={label}>{content}</span>;
}

/** The Document ID of a viewer link on a data field (`/viewer/<id>`); anything else is not previewed. */
function viewerDocumentId(href: string): string {
  return /^\/viewer\/([A-Za-z0-9]+)$/.exec(href)?.[1] ?? "";
}

function DocumentThumbnail({ documentId }: { documentId: string }) {
  const [thumbnail, setThumbnail] = useState("");
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let active = true;
    let objectUrl = "";
    setThumbnail("");
    setFailed(false);
    const load = async () => {
      try {
        objectUrl = await api.documents.thumbnail(documentId, { cache: true });
        if (active) setThumbnail(objectUrl);
        else if (objectUrl.startsWith("blob:")) URL.revokeObjectURL(objectUrl);
      } catch {
        if (active) setFailed(true);
      }
    };
    void load();
    return () => {
      active = false;
      if (objectUrl.startsWith("blob:")) URL.revokeObjectURL(objectUrl);
    };
  }, [documentId]);
  return thumbnail && !failed
    ? <img src={thumbnail} alt="" loading="lazy" onError={() => setFailed(true)} />
    : <IconPlay size={14} aria-hidden="true" />;
}

function DocumentPreviewBody({ documentId }: { documentId: string }) {
  const [url, setUrl] = useState("");
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let active = true;
    let objectUrl = "";
    const load = async () => {
      try {
        objectUrl = await api.documents.preview(documentId);
        if (active) setUrl(objectUrl);
        else URL.revokeObjectURL(objectUrl);
      } catch {
        if (active) setFailed(true);
      }
    };
    void load();
    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [documentId]);
  if (failed) return <p role="alert">{t("component.result_files.preview_failed")}</p>;
  if (!url) return <p role="status">{t("component.result_files.loading_preview")}</p>;
  return <video className="workspace-app-data__document-video" src={url} controls playsInline preload="metadata"
    aria-label={t("component.workspace_app_view.data_preview_document")} onError={() => setFailed(true)} />;
}

/** A compact card for a document on a data field; its player and the open action live in the global detail pop-up. */
function DocumentPreviewCard({ documentId, href }: { documentId: string; href: string }) {
  const navigate = useNavigate();
  const title = t("component.workspace_app_view.data_preview_document");
  return <CompactCard
    icon={<span className="workspace-app-data__document-thumb"><DocumentThumbnail documentId={documentId} /></span>}
    title={title}
    ariaLabel={title}
    onClick={() => openDetail({
      title,
      body: <DocumentPreviewBody documentId={documentId} />,
      primaryAction: {
        label: t("component.workspace_app_view.data_open_document"),
        onClick: () => {
          closeDetail();
          navigate(href);
        },
      },
    })}
  />;
}

function RecordFiles({ media }: { media: NonNullable<WorkspaceAppDataItem["media"]> }) {
  if (!media.length) return null;
  return <details className="workspace-app-data__record-files">
    <summary>{t("component.workspace_app_view.data_files")}</summary>
    <span>{media.map((item) => {
      const href = item.viewer_url && isSafeHref(item.viewer_url) ? item.viewer_url : null;
      const label = item.label || item.title || t("component.workspace_app_view.data_document");
      return href ? <a key={item.document_id} href={href}>{label}</a> : <span key={item.document_id}>{label}</span>;
    })}</span>
  </details>;
}

function RecordItems({ workspaceId, items, documents = false, blockTitle, compactDocuments = false }: { workspaceId: string; items: WorkspaceAppDataItem[]; documents?: boolean; blockTitle?: string | null; compactDocuments?: boolean }) {
  if (!items.length) return null;
  const singleDocument = documents && items.length === 1 && blockTitle;
  const dates = items.flatMap((item) => [item.updated_at, item.published_at,
    ...(item.fields || []).filter((field) => field.format === "date").map((field) => field.value)])
    .filter((value): value is string => typeof value === "string");
  return <div className="workspace-app-data__items">{items.map((item, index) => {
    const filename = item.title || item.label || t("component.workspace_app_view.data_untitled");
    const generatedFilename = documents && /(?:^|[-_\s])[0-9A-HJKMNP-TV-Z]{26}(?:[-_.\s]|$)/i.test(filename);
    const hostname = !documents ? publicUrlHostname(item.title) : null;
    const publicationLabel = platformLabel(item.platform);
    const hasPlatform = !documents && Boolean(item.platform);
    const providerField = item.fields?.some((field) => field.format === "provider");
    const primary = singleDocument ? blockTitle : generatedFilename && item.updated_at
      ? `${blockTitle || t("component.workspace_app_view.data_document")} · ${formatDateCompact(item.updated_at, dates)}`
      : hostname && hasPlatform ? `${publicationLabel} ${t("component.workspace_app_view.data_publication")}` : filename;
    const secondary = generatedFilename || singleDocument ? null : hostname || (hasPlatform ? publicationLabel : item.subtitle) || item.detail;
    const primaryTitle = documents ? filename : hostname ? item.title || undefined : primary;
    const trackingStatus = item.tracking_status ? t(`component.workspace_app_view.data_tracking_${item.tracking_status}`) : null;
    const hasMetrics = Boolean(item.metrics?.length);
    const imageMedia = item.media?.filter((media) => String(media.mime_type || "").toLowerCase().startsWith("image/")) || [];
    const fileMedia = item.media?.filter((media) => !String(media.mime_type || "").toLowerCase().startsWith("image/")) || [];
    const record = <>
      {documents && <span className="workspace-app-data__item-icon" aria-hidden="true"><IconDocument size={16} /></span>}
      <span className="workspace-app-data__item-copy">
        <strong title={primaryTitle}>{primary}</strong>{secondary && !hasMetrics && !hasPlatform && <small title={secondary}>{secondary}</small>}
        {(hasMetrics || item.published_at || hasPlatform) && <span className="workspace-app-data__record-meta">
          {hasPlatform && !providerField && <PlatformLabel value={item.platform!} />}
          {hasMetrics && secondary && (!hasPlatform || secondary !== publicationLabel) && <span>{secondary}</span>}
          {item.published_at && <span>{t("component.workspace_app_view.data_published")} <time dateTime={item.published_at} title={formatDateLong(item.published_at)}>{relativeTime(item.published_at)}</time></span>}
        </span>}
        {item.fields?.length ? <AppDataFields fields={item.fields} dates={dates} className="workspace-app-data__fields" humanizeText /> : null}
        {item.conflict && <span className="workspace-app-data__error">{t("component.workspace_app_view.data_publication_conflict", { detail: item.conflict })}</span>}
      </span>
      {!hasMetrics && (item.status || trackingStatus) && <span className="workspace-app-data__record-statuses">
        {item.status && <span className={`workspace-app-data__status ${statusClass(item.status)}`}>{humanize(item.status)}</span>}
        {trackingStatus && <span className="workspace-app-data__status">{trackingStatus}</span>}
      </span>}
      {!item.metrics?.length && item.updated_at && <time dateTime={item.updated_at} title={formatDateLong(item.updated_at)}>{relativeTime(item.updated_at)}</time>}
    </>;
    // A block pinned to one document (a list kept in Knowledge) shows its content.
    const inlineKind = documents && items.length === 1 && item.document_id ? inlineDocumentTextKind(item.title) : null;
    if (!compactDocuments && inlineKind && item.document_id) return <article key={item.id || `${filename}:${index}`} className="workspace-app-data__record">
      <ItemLink item={item} className="workspace-app-data__record-heading">{record}</ItemLink>
      <InlineDocumentText documentId={item.document_id} kind={inlineKind} label={filename} live />
    </article>;
    if (!item.metrics?.length && !imageMedia.length && !fileMedia.length && !item.execution) return <ItemLink key={item.id || `${filename}:${index}`} item={item}>{record}</ItemLink>;
    return <article key={item.id || `${filename}:${index}`} className="workspace-app-data__record">
      {imageMedia.length ? <RecordMedia media={imageMedia} /> : null}
      <ItemLink item={item} className="workspace-app-data__record-heading">{record}</ItemLink>
      {(item.execution || fileMedia.length > 0) && <div className="workspace-app-data__record-tools">
        {item.execution && <ExecutionDetails workspaceId={workspaceId} execution={item.execution} labelKey="latest_related_run" />}
        <RecordFiles media={fileMedia} />
      </div>}
      {item.tracking_status && item.tracking_status !== "configured" && <p className="workspace-app-data__tracking-notice">{trackingStatus}</p>}
      {item.metrics?.length ? <MetricItems items={item.metrics} linked={false} /> : null}
    </article>;
  })}</div>;
}

function MetricItems({ items, linked = true, presentation = "full" }: { items: WorkspaceAppDataMetric[]; linked?: boolean; presentation?: "full" | "compact" | "details" }) {
  if (!items.length) return null;
  const compact = presentation === "compact";
  const detailsOnly = presentation === "details";
  const first = items[0];
  const sharedSubtitle = first.subtitle && items.every((item) => item.subtitle === first.subtitle) ? first.subtitle : null;
  const missingValue = (item: WorkspaceAppDataMetric) => item.value === null || item.value === undefined || item.value === "";
  const presentationKey = (item: WorkspaceAppDataMetric) => `${item.freshness || item.status || ""}:${item.collection_error || ""}:${missingValue(item)}`;
  const sharedPresentation = items.length > 1 && items.every((item) => presentationKey(item) === presentationKey(first));
  const sharedObservation = sharedPresentation && Boolean(first.updated_at) && items.every((item) => item.updated_at === first.updated_at);
  const latestObservedAt = detailsOnly ? items.reduce<string | null>((latest, item) => {
    if (!item.updated_at || !Number.isFinite(Date.parse(item.updated_at))) return latest;
    return !latest || Date.parse(item.updated_at) > Date.parse(latest) ? item.updated_at : latest;
  }, null) : sharedObservation ? first.updated_at : null;
  const sharedAttention = sharedPresentation ? workspaceStatAttentionPresentation(first.freshness || first.status || (missingValue(first) ? "no_data" : "fresh")) : null;
  const sharedError = sharedPresentation ? first.collection_error : null;
  return <div className={`workspace-app-data__statistics${presentation !== "full" ? ` is-${presentation}` : ""}`}>
    <div className="workspace-app-data__metrics">{items.map((item, index) => {
      const missing = missingValue(item);
      const attention = workspaceStatAttentionPresentation(item.freshness || item.status || (missing ? "no_data" : "fresh"));
      const visibleAttention = sharedPresentation ? null : attention;
      const deltaHint = item.delta_reason && item.delta_reason !== "missing_comparable_baseline"
        ? t(`component.workspace_app_view.data_delta_${item.delta_reason}`) : null;
      const valueHint = [...new Set([deltaHint, ...((missing || compact) ? [item.collection_error, attention?.label] : []),
        compact && item.updated_at ? `${t("component.workspace_app_view.data_latest_collection")} ${formatDateLong(item.updated_at)}` : null].filter(Boolean))].join(" ");
      const visibleError = sharedPresentation ? null : item.collection_error;
      const change = item.change;
      const hasChange = presentation === "full" && !missing && !item.delta_reason && typeof change === "number" && Number.isFinite(change);
      const showMeta = hasChange || visibleAttention || (linked && !sharedSubtitle && item.subtitle) || visibleError || (!detailsOnly && !sharedObservation && item.updated_at);
      if (detailsOnly && !showMeta) return null;
      const content = <>
        <span className="workspace-app-data__metric-copy">
          <small title={metricLabel(item)}>{metricLabel(item)}</small>
          {!detailsOnly && <strong title={valueHint || undefined} tabIndex={valueHint ? 0 : undefined}
            aria-description={valueHint ? `${metricLabel(item)}: ${metricValue(item)}. ${valueHint}` : undefined}>{metricValue(item)}</strong>}
          {!compact && showMeta && <span className="workspace-app-data__metric-meta">
            {hasChange && <b className={change > 0 ? "is-positive" : change < 0 ? "is-negative" : undefined}>{change > 0 ? "+" : ""}{change.toLocaleString()}</b>}
            {visibleAttention && <em className={visibleAttention.type === "danger" ? "workspace-app-data__error" : "workspace-app-data__stale"}>{visibleAttention.label}</em>}
            {linked && !sharedSubtitle && item.subtitle && <PlatformLabel value={item.subtitle} />}
            {visibleError && <span className="workspace-app-data__error">{visibleError}</span>}
            {!detailsOnly && !sharedObservation && item.updated_at && <time dateTime={item.updated_at} title={formatDateLong(item.updated_at)}>{t("component.workspace_app_view.data_latest_collection")} {relativeTime(item.updated_at)}</time>}
          </span>}
        </span>
      </>;
      return linked ? <ItemLink key={item.id || `${metricLabel(item)}:${index}`} item={item} className="workspace-app-data__metric">{content}</ItemLink> : <div key={item.id || `${metricLabel(item)}:${index}`} className="workspace-app-data__metric">{content}</div>;
    })}</div>
    {!compact && ((linked && sharedSubtitle) || latestObservedAt || sharedAttention || sharedError) && <div className="workspace-app-data__metric-footer">
      {latestObservedAt && <time dateTime={latestObservedAt} title={formatDateLong(latestObservedAt)}>{t("component.workspace_app_view.data_latest_collection")} {relativeTime(latestObservedAt)}</time>}
      {sharedAttention && <em className={sharedAttention.type === "danger" ? "workspace-app-data__error" : "workspace-app-data__stale"}>{sharedAttention.label}</em>}
      {sharedError && <span className="workspace-app-data__error">{sharedError}</span>}
      {linked && sharedSubtitle && <PlatformLabel value={sharedSubtitle} />}
    </div>}
  </div>;
}

/** A run's control state, and whether it is paused on a person who may answer it here. */
function useRunControl(workspaceId: string, runId: string, enabled = true) {
  const controlQuery = useQuery({
    queryKey: ["workflow-run-history-control", runId],
    queryFn: ({ signal }) => api.workflows.getRun(runId, false, signal),
    enabled: Boolean(workspaceId && runId) && enabled,
    staleTime: 15_000,
    refetchInterval: 30_000,
  });
  const run = controlQuery.data as WorkflowHistoryRun | undefined;
  const current = run?.id === runId && run.workspace_id === workspaceId ? run : undefined;
  const action = current?.intervention;
  // Compact responses carry pending requests in intervention; the dialog
  // fetches full detail and revalidates resume_action before allowing a response.
  const responseAllowed = !controlQuery.isError && current?.status === "paused"
    && current.capabilities?.can_control === true && Boolean(current.resume_action
      || ((action?.kind === "workflow_approval" || action?.kind === "workflow_input") && action.options?.length));
  return { controlQuery, current, action, responseAllowed };
}

/** A calendar post whose run waits on the person: answer it from the post itself. */
function CalendarItemResponse({ workspaceId, item }: { workspaceId: string; item: WorkspaceAppDataItem }) {
  const runId = item.execution?.run_id || "";
  const paused = String(item.execution?.status || "").toLowerCase() === "paused";
  const { action, responseAllowed } = useRunControl(workspaceId, runId, paused);
  if (!paused || !responseAllowed) return null;
  return <WorkflowRunResumeControl runId={runId} variant="primary"
    label={t(action?.kind === "workflow_approval" ? "component.workspace_app_view.calendar_review" : "component.workspace_app_view.calendar_respond")} />;
}

function ExecutionRecovery({ workspaceId, runId, href, answeredElsewhere = false }: {
  workspaceId: string; runId: string; href: string | null;
  /** The response already sits beside this run (a calendar post's row), so it is not offered twice. */
  answeredElsewhere?: boolean;
}) {
  const queryClient = useQueryClient();
  const [retryStarted, setRetryStarted] = useState(false);
  const { controlQuery, current, action, responseAllowed } = useRunControl(workspaceId, runId, !retryStarted);
  const recoverable = current?.status === "failed" || current?.status === "cancelled";
  const effectReviewAllowed = !controlQuery.isError && recoverable
    && current.capabilities?.can_control === true && action?.kind === "workflow_effect_review"
    && action.options?.includes("review");
  const retryAllowed = !controlQuery.isError && recoverable
    && current.capabilities?.can_control === true
    && action?.kind === "workflow_retry" && action.options?.includes("retry")
    && Boolean(action.retry_from_step_id || action.step_id)
    && workflowRetrySchemaIsCompatible(action.editable_input_schema);
  const reviewRequired = !controlQuery.isError && recoverable
    && current.capabilities?.can_control === true && !retryAllowed && !effectReviewAllowed;
  return <div className="workspace-app-data__recovery">
    {controlQuery.isError && <span>{t("component.workspace_app_view.data_recovery_load_error")}</span>}
    <div className="workspace-app-data__recovery-actions">
      {responseAllowed && !answeredElsewhere && <WorkflowRunResumeControl runId={runId} />}
      {retryStarted ? <span role="status">{t("component.workspace_app_view.data_retry_started")}</span> : (retryAllowed || effectReviewAllowed || reviewRequired) && current && <WorkflowRunRetryControl
        runId={runId}
        workspaceId={workspaceId}
        bindingId={current.binding_id}
        workflowId={current.workflow_id}
        size="sm"
        reviewRequired={Boolean(effectReviewAllowed)}
        onRetried={() => {
          setRetryStarted(true);
          void queryClient.invalidateQueries({ queryKey: ["workspace-app-data", workspaceId] });
          void queryClient.invalidateQueries({ queryKey: ["workspace-app-data-contexts", workspaceId] });
        }}
      />}
      {href && <a href={href} className={reviewRequired ? "workspace-app-data__review-action" : undefined}>
        {t(reviewRequired ? "component.workspace_app_view.data_review_run" : "component.workspace_app_view.data_open_run")}
      </a>}
    </div>
  </div>;
}

function ExecutionDetails({ workspaceId, execution, labelKey, detail, answeredElsewhere }: {
  workspaceId: string;
  answeredElsewhere?: boolean;
  execution: NonNullable<WorkspaceAppDataItem["execution"]>;
  labelKey?: "latest_related_run";
  detail?: string | null;
}) {
  const status = String(execution.status || "").toLowerCase();
  const needsAttention = ["failed", "paused", "cancelled"].includes(status);
  const terminal = ["completed", "success"].includes(status);
  const href = execution.href && isSafeHref(execution.href) ? execution.href : null;
  const label = t(labelKey ? `component.workspace_app_view.data_${labelKey}` : `component.workspace_app_view.data_${status === "completed" ? "recent_execution" : "execution"}`);
  if (status === "failed") return <div className="workspace-app-data__execution is-failed">
    <details className="workspace-app-data__failure-details">
      <summary>
        <span className="workspace-app-data__error"><b>{label}</b> {stateLabel(execution.status)}</span>
        <span>{t("component.workspace_app_view.data_failure_details")}</span>
        <IconArrowRight size={13} aria-hidden="true" />
      </summary>
      <div className="workspace-app-data__failure-body">
        {(execution.current_step_id || detail) && <div className="workspace-app-data__failure-context">
          {execution.current_step_id && <span>{humanize(execution.current_step_id)}</span>}
          {detail && <span>{humanize(detail)}</span>}
        </div>}
        {execution.error && <p>{execution.error}</p>}
      </div>
    </details>
    <ExecutionRecovery key={execution.run_id} workspaceId={workspaceId} runId={execution.run_id} href={href} />
  </div>;
  if (status === "cancelled") return <div className="workspace-app-data__execution is-failed">
    <span className="workspace-app-data__error"><b>{label}</b> {stateLabel(execution.status)}</span>
    {execution.current_step_id && <span>{humanize(execution.current_step_id)}</span>}
    {detail && <span>{humanize(detail)}</span>}
    {execution.error && <span className="workspace-app-data__execution-error" title={execution.error}>{execution.error}</span>}
    <ExecutionRecovery key={execution.run_id} workspaceId={workspaceId} runId={execution.run_id} href={href} />
  </div>;
  const content = <div className="workspace-app-data__execution">
    <span className={needsAttention ? "workspace-app-data__error" : statusClass(status)}><b>{label}</b> {stateLabel(execution.status)}</span>
    {execution.current_step_id && status !== "completed" && <span>{humanize(execution.current_step_id)}</span>}
    {detail && <span>{humanize(detail)}</span>}
    {execution.error && (isLongText(execution.error)
      ? <LongSummaryValue value={execution.error} className="workspace-app-data__execution-error-disclosure" />
      : <span className="workspace-app-data__error workspace-app-data__execution-error" title={execution.error}>{execution.error}</span>)}
    {status === "paused"
      ? <ExecutionRecovery key={execution.run_id} workspaceId={workspaceId} runId={execution.run_id} href={href} answeredElsewhere={answeredElsewhere} />
      : href && <a href={href}>{t("component.workspace_app_view.data_open_run")}</a>}
  </div>;
  if (!terminal) return content;
  return <details className="workspace-app-data__execution-disclosure">
    <summary>{labelKey ? label : t("component.workspace_app_view.data_execution_details")}</summary>
    {content}
  </details>;
}

// Rollups count an unfinished item by its run; these read as the calendar reads them.
const EXECUTION_STAGE_LABELS: Record<string, string> = {
  failed: "failed", cancelled: "cancelled", paused: "needs_attention", pending: "waiting",
};

function configuredStageLabel(stage: string, labels?: Record<string, string>): string {
  const execution = EXECUTION_STAGE_LABELS[stage];
  return labels?.[stage] || (execution ? t(`component.workspace_app_view.${execution}`) : stateLabel(stage));
}

function ProgressItems({ workspaceId, items, stageLabels }: { workspaceId: string; items: WorkspaceAppDataItem[]; stageLabels?: Record<string, string> }) {
  if (!items.length) return null;
  return <ol className="workspace-app-data__progress">{items.map((item, index) => {
    const execution = item.execution;
    const stoppedLabel = execution?.status === "failed" ? t("component.workspace_app_view.failed")
      : execution?.status === "cancelled" ? t("component.workspace_app_view.cancelled") : null;
    const dates = [item.updated_at, item.last_run_at, item.next_run_at, item.stop_after, ...(item.fields || []).map((field) => field.value)]
      .filter((value): value is string => typeof value === "string");
    return <li key={item.id || `${item.label}:${index}`}>
      <div className="workspace-app-data__progress-copy">
        <div className="workspace-app-data__progress-heading">
          <strong title={item.label || item.title || undefined}>{humanize(item.label || item.title) || t("component.workspace_app_view.data_untitled")}</strong>
          {(item.status || stoppedLabel) && <em
            className={stoppedLabel ? "workspace-app-data__status is-error" : undefined}
            title={stoppedLabel && item.status ? configuredStageLabel(item.status, stageLabels) : undefined}
          >{stoppedLabel || configuredStageLabel(item.status!, stageLabels)}</em>}
        </div>
        {item.detail && !execution && !sameDisplayLabel(item.detail, item.label) && <small>{humanize(item.detail)}</small>}
        {item.fields?.length ? <AppDataFields fields={item.fields} dates={dates} className="workspace-app-data__progress-fields" /> : null}
        {item.enabled !== undefined && <span className="workspace-app-data__progress-fields">
          <span><b>{t("component.workspace_app_view.data_automation_state")}</b> {t(`component.workspace_app_view.data_automation_${item.state || (item.enabled ? "enabled" : "disabled")}`)}</span>
          {item.last_run_at && <span><b>{t("component.workspace_app_view.data_last_run")}</b> <time dateTime={item.last_run_at} title={formatDateLong(item.last_run_at)}>{formatDateCompact(item.last_run_at, dates)}</time>{item.last_status ? ` · ${stateLabel(item.last_status)}` : ""}</span>}
          {item.next_run_at && <span><b>{t("component.workspace_app_view.data_next_check")}</b> <time dateTime={item.next_run_at} title={formatDateLong(item.next_run_at)}>{formatDateCompact(item.next_run_at, dates)}</time></span>}
          {item.stop_after && <span><b>{t("component.workspace_app_view.data_schedule_ends")}</b> <time dateTime={item.stop_after} title={formatDateLong(item.stop_after)}>{formatDateCompact(item.stop_after, dates)}</time></span>}
        </span>}
        {execution && <ExecutionDetails workspaceId={workspaceId} execution={execution} detail={item.detail} />}
      </div>
    </li>;
  })}</ol>;
}

function RollupItems({ workspaceId, items, showNext, completion, stageLabels, emptyMessage }: {
  workspaceId: string;
  items: WorkspaceAppDataItem[];
  showNext: boolean;
  completion?: { stages: string[]; label: string };
  stageLabels?: Record<string, string>;
  emptyMessage?: string | null;
}) {
  if (!items.length) return null;
  const populatedItems = items.filter((item) => item.total !== 0 || Object.values(item.counts_by_stage || {}).some((count) => count > 0));
  if (!populatedItems.length && emptyMessage) return <p className="workspace-app-data__empty">{emptyMessage}</p>;
  return <div className="workspace-app-data__rollups">{(populatedItems.length ? populatedItems : items).map((item) => {
    const next = item.next;
    const nextExecution = next?.execution;
    const dates = next?.fields.map((field) => field.value).filter((value): value is string => typeof value === "string") || [];
    const stageCounts = Object.entries(item.counts_by_stage || {});
    const completeStages = new Set(completion?.stages || []);
    const completedCount = completion ? stageCounts.reduce((total, [stage, count]) => total + (completeStages.has(stage) ? count : 0), 0) : null;
    const incompleteStages = completion ? stageCounts.filter(([stage, count]) => !completeStages.has(stage) && count > 0) : stageCounts;
    const completionSummary = completion && item.total !== undefined && item.total > 0;
    const singleStageTotal = stageCounts.length === 1 && item.total !== undefined && stageCounts[0][1] === item.total;
    return <article key={item.id} className={`workspace-app-data__rollup${showNext ? "" : " is-count-only"}${singleStageTotal && showNext ? " is-next-stage-only" : ""}${completionSummary ? " is-completion-summary" : ""}`}>
      {completionSummary ? <div className="workspace-app-data__rollup-completion"><strong>{completedCount!.toLocaleString()} / {item.total!.toLocaleString()}</strong><span>{completion!.label}</span></div>
        : !singleStageTotal && <div className="workspace-app-data__rollup-total"><strong>{item.total ?? 0}</strong><span>{t("component.workspace_app_view.data_total_items")}</span></div>}
      {incompleteStages.length > 0 && <dl className="workspace-app-data__rollup-counts">
        {incompleteStages.map(([stage, count]) => <div key={stage}><dt>{configuredStageLabel(stage, stageLabels)}</dt><dd>{count.toLocaleString()}</dd></div>)}
      </dl>}
      {showNext && (next ? <div className="workspace-app-data__rollup-next">
        <small>{t("component.workspace_app_view.data_earliest_pending")}</small>
        <strong>{next.label}</strong>
        <span className={`workspace-app-data__status ${statusClass(next.stage)}`}>{configuredStageLabel(next.stage, stageLabels)}</span>
        {next.fields.length > 0 && <AppDataFields fields={next.fields} dates={dates} className="workspace-app-data__progress-fields" />}
        {nextExecution && <ExecutionDetails workspaceId={workspaceId} execution={nextExecution} />}
      </div> : <p className="workspace-app-data__empty">{t("component.workspace_app_view.data_no_pending")}</p>)}
    </article>;
  })}</div>;
}

function summaryValue(field: NonNullable<WorkspaceAppDataItem["fields"]>[number], dates: string[], humanizeText = false) {
  if (field.format === "link" && field.href && isSafePublicHref(field.href)) return <a href={field.href} target="_blank" rel="noreferrer">{String(field.value || field.href)}</a>;
  if (field.format === "document" && field.href && isSafeHref(field.href)) {
    const documentId = viewerDocumentId(field.href);
    return documentId
      ? <DocumentPreviewCard documentId={documentId} href={field.href} />
      : <a href={field.href}>{t("component.workspace_app_view.data_open_document")}</a>;
  }
  if (field.format === "link_list" && field.links?.length) {
    const links = field.links.filter((link) => isSafePublicHref(link.href));
    return links.length ? <span className="workspace-app-data__summary-links">{links.map((link) => <a key={link.href} href={link.href} target="_blank" rel="noreferrer">{link.label}</a>)}</span> : "—";
  }
  if (field.format === "date" && typeof field.value === "string" && !Number.isNaN(Date.parse(field.value))) return <time dateTime={field.value} title={formatDateLong(field.value)}>{formatDateCompact(field.value, dates)}</time>;
  if (field.format === "provider") return <ProviderValue provider={field.provider} label={typeof field.value === "string" ? field.value : null} secondary={field.secondary} />;
  if (field.format === "status") return <span className={`workspace-app-data__status ${statusClass(String(field.value || ""))}`}>{humanize(String(field.value || "—"))}</span>;
  const text = humanizeText && typeof field.value === "string" ? humanize(field.value) : String(field.value ?? "—");
  return field.secondary ? <>{text} <small className="workspace-app-data__platform-account">{field.secondary}</small></> : text;
}

function isLongSummaryField(field: NonNullable<WorkspaceAppDataItem["fields"]>[number]): boolean {
  return !["date", "status", "link", "link_list", "document", "provider"].includes(field.format || "text")
    && typeof field.value === "string" && isLongText(field.value);
}

function LongSummaryValue({ value, className = "" }: { value: string; className?: string }) {
  const preview = value.length > SUMMARY_PREVIEW_LENGTH ? `${value.slice(0, SUMMARY_PREVIEW_LENGTH).trimEnd()}...` : value;
  return <details className={`workspace-app-data__summary-disclosure ${className}`.trim()}>
    <summary>
      <span className="workspace-app-data__summary-preview">{preview}</span>
      <span className="workspace-app-data__summary-show-more">{t("chat.show_more")}</span>
      <span className="workspace-app-data__summary-show-less">{t("chat.show_less")}</span>
    </summary>
    <span className="workspace-app-data__summary-full">{value}</span>
  </details>;
}

function AppDataFields({ fields, dates, className, humanizeText = false }: { fields: NonNullable<WorkspaceAppDataItem["fields"]>; dates: string[]; className: string; humanizeText?: boolean }) {
  return <div className={className}>{fields.map((field) => {
    const long = isLongSummaryField(field);
    return <div key={field.label} className={long ? "workspace-app-data__field workspace-app-data__field--long" : "workspace-app-data__field"}>
      <b>{field.label}</b> {long && typeof field.value === "string" ? <LongSummaryValue value={field.value} /> : summaryValue(field, dates, humanizeText)}
    </div>;
  })}</div>;
}

function SummaryItems({ items }: { items: WorkspaceAppDataItem[] }) {
  const fields = items.flatMap((item) => item.fields || []);
  if (!fields.length) return null;
  const dates = fields.filter((field) => field.format === "date")
    .map((field) => field.value).filter((value): value is string => typeof value === "string");
  return <dl className="workspace-app-data__summary">{fields.map((field) => {
    const long = isLongSummaryField(field);
    return <div key={field.label} className={long ? "workspace-app-data__summary-item--long" : undefined}>
      <dt>{field.label}</dt><dd>{long && typeof field.value === "string" ? <LongSummaryValue value={field.value} /> : summaryValue(field, dates)}</dd>
    </div>;
  })}</dl>;
}

/** A project's files under the folders they are in, as the Workspace files show them. */
function FolderItems({ workspaceId, items, blockTitle, compactDocuments = false }: { workspaceId: string; items: WorkspaceAppDataItem[]; blockTitle?: string | null; compactDocuments?: boolean }) {
  const folders = new Map<string, WorkspaceAppDataItem[]>();
  for (const item of items) folders.set(item.folder || "", [...(folders.get(item.folder || "") || []), item]);
  return <div className="workspace-app-data__folders">{[...folders].map(([folder, files]) => <div key={folder} className="workspace-app-data__folder">
    <p className="workspace-app-data__folder-path"><IconFolder size={14} aria-hidden="true" />
      <span className="mono">{folder ? folder.split("/").join(" / ") : t("component.workspace_app_view.data_project_folder")}</span></p>
    <RecordItems workspaceId={workspaceId} items={files} documents blockTitle={files.length === 1 ? null : blockTitle} compactDocuments={compactDocuments} />
  </div>)}</div>;
}

function versionNumber(version?: string): number {
  const match = /^v(\d+)$/.exec(version || "");
  return match ? Number(match[1]) : 0;
}

type PackRun = NonNullable<NonNullable<WorkspaceAppDataItem["pack"]>["run"]>;

type Pack = {
  id: string;
  label: string;
  stage: string | null;
  /** The stored stage the label shows ("planned" before the child exists). */
  stageKey?: string | null;
  synopsis?: string;
  /** A planned child's code and parent: what its own action opens with. */
  code?: string;
  parentId?: string;
  /** Its latest run while it is being made or after it stopped; done runs show the stage. */
  run?: PackRun;
  cover?: WorkspaceAppDataItem;
  /** An episode with no picture shows a frame of its video. */
  coverIsVideo?: boolean;
  artifacts: ReviewArtifactReference[];
};

/**
 * Each child project (an episode) with only its latest delivered version's
 * results: a newer version still being made never hides what was delivered.
 */
function packsOf(items: WorkspaceAppDataItem[], stageLabels?: Record<string, string>): Pack[] {
  const grouped = new Map<string, WorkspaceAppDataItem[]>();
  for (const item of items) if (item.pack) grouped.set(item.pack.id, [...(grouped.get(item.pack.id) || []), item]);
  return [...grouped.values()].map((rows) => {
    const pack = rows[0].pack!;
    // A child's card row has no file; it only says the child exists.
    const files = rows.filter((row) => row.document_id);
    const run = rows.find((row) => row.pack?.run)?.pack?.run;
    const making = run && !["completed", "cancelled"].includes(run.status) ? run : undefined;
    const delivered = files.filter((file) => file.delivered);
    const candidates = delivered.length ? delivered : files;
    const latest = Math.max(...candidates.map((file) => versionNumber(file.pack?.version)));
    const shown = candidates.filter((file) => versionNumber(file.pack?.version) === latest);
    const artifacts = shown.map((file) => ({
      key: file.id || file.title || "", label: file.title || "", reference: file.document_id || "",
      documentId: file.document_id || undefined, kind: artifactKind(null, file.title || ""),
    }));
    const image = shown.findIndex((_file, index) => artifacts[index].kind === "image");
    const cover = image >= 0 ? image : shown.findIndex((_file, index) => artifacts[index].kind === "video");
    return {
      id: pack.id, label: pack.label, stageKey: pack.stage, synopsis: pack.synopsis, code: pack.code, parentId: pack.parent_id,
      // While it is being made (or stopped) its run says how it is going; a
      // run paused only for background work is still running.
      stage: making ? runStatusLabel(making.status === "paused" && !making.needs_you ? "pending" : making.status)
        : pack.stage ? stageLabels?.[pack.stage] || humanize(pack.stage) : null,
      run: making,
      cover: cover >= 0 ? shown[cover] : undefined,
      coverIsVideo: cover >= 0 && image < 0,
      artifacts,
    };
  });
}

function PackCover({ documentId, label, video = false }: { documentId?: string | null; label: string; video?: boolean }) {
  const thumbnail = useQuery({
    queryKey: ["workspace-app-pack-cover", documentId, video],
    queryFn: () => video
      ? api.documents.videoThumbnail(documentId!, { cache: true })
      : api.documents.imageThumbnail(documentId!, { cache: true }),
    enabled: Boolean(documentId),
    staleTime: Infinity,
    retry: false,
  });
  return thumbnail.data
    ? <img src={thumbnail.data} alt="" />
    : <span className="workspace-app-data__pack-placeholder" aria-hidden="true">{label.slice(0, 3)}</span>;
}

/** What one pack holds, read in the global detail drawer: the picture, the recording, the script. */
function PackDetail({ pack, openRun, action, onAction }: { pack: Pack; openRun: WorkspaceAppRunOpener | null; action?: PackAction; onAction: (action: PackAction) => void }) {
  const players = pack.artifacts.filter((artifact) => artifact.kind === "audio" || artifact.kind === "video");
  const texts = pack.artifacts.filter((artifact) => artifact.kind === "text");
  const run = pack.run;
  const cover = pack.artifacts.find((artifact) => artifact.documentId && artifact.documentId === pack.cover?.document_id);
  return <div className="workspace-app-data__pack-detail">
    {pack.synopsis && <p className="workspace-app-data__pack-synopsis">{pack.synopsis}</p>}
    {action && <div className="workspace-app-data__pack-action">
      <Button size="sm" onClick={() => onAction(action)}>
        {action.planned ? <IconPlay size={14} /> : <IconRefresh size={14} />}
        <span>{action.label}</span>
      </Button>
    </div>}
    {run && openRun && <div className="workspace-app-data__pack-progress">
      <span>{pack.stage}</span>
      <Button size="sm" variant="outline" onClick={() => { closeDetail(); openRun(run); }}>
        {t("component.workspace_app_view.data_pack_progress")}
      </Button>
    </div>}
    {pack.cover && !pack.coverIsVideo && <PackMedia artifact={cover}>
      <div className="workspace-app-data__pack-detail-cover"><PackCover documentId={pack.cover.document_id} label={pack.label} /></div>
    </PackMedia>}
    {players.map((artifact) => <PackMedia key={artifact.key} artifact={artifact}><ReviewPlayer artifact={artifact} /></PackMedia>)}
    {texts.map((artifact) => <PackMedia key={artifact.key} artifact={artifact}><ReviewText artifact={artifact} /></PackMedia>)}
  </div>;
}

/** One file of a pack, with its own download in the corner: what is saved is what is shown. */
function PackMedia({ artifact, children }: { artifact?: ReviewArtifactReference; children: ReactNode }) {
  return <div className="workspace-app-data__pack-media">
    {children}
    {artifact?.documentId && <PackDownload artifact={artifact} />}
  </div>;
}

const PACK_CORNER_BUTTON = { width: 36, height: 36, padding: 0, borderRadius: "50%" } as const;

/** A file of a pack saved to the device; a recording already played downloads from its local copy. */
function PackDownload({ artifact }: { artifact: ReviewArtifactReference }) {
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    try {
      const url = await api.documents.download(artifact.documentId!, { cache: true });
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = artifact.label || "file";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } finally {
      setBusy(false);
    }
  };
  const label = t("component.workspace_app_view.data_pack_download", { name: artifact.label || "" });
  // Button's own small size sets a height and padding inline; every corner
  // button is the same circle.
  return <Button size="sm" variant="ghost" className="workspace-app-data__pack-download" style={PACK_CORNER_BUTTON}
    loading={busy} disabled={busy} title={label} ariaLabel={label} ariaBusy={busy} onClick={() => void save()}>
    {!busy && <IconDownload size={18} />}
  </Button>;
}

/** Projects to pick from as cards: a cover, the name, how many children are done. */
function ContextCards({ contexts, onOpen }: { contexts: WorkspaceAppDataContext[]; onOpen: (context: WorkspaceAppDataContext) => void }) {
  return <div className="workspace-app-data__packs">{contexts.map((item) => (
    <button key={`${item.kind}:${item.id}`} type="button" className="workspace-app-data__pack" onClick={() => onOpen(item)}>
      <span className="workspace-app-data__pack-cover"><PackCover documentId={item.cover?.document_id} label={item.label} /></span>
      <span className="workspace-app-data__pack-caption">
        <strong title={item.label}>{item.label}</strong>
        {item.progress && <small>{item.progress.done} / {item.progress.total} · {item.progress.label}</small>}
      </span>
    </button>
  ))}</div>;
}

/** Child projects as cover cards; one opens its results in the detail drawer. */
type BlockConfig = WorkspaceAppDataComponent["blocks"][number];
/** What one pack's button opens: an App entry and the inputs it starts with. */
type PackAction = { action: string; label: string; values: Record<string, string>; planned: boolean };

/** One card per result a Flow made, its latest version; a card opens the run that made it. */
// A track's sleeve: its own picture when the run made one, otherwise colour
// drawn from the title so every record looks like itself.
function RecordSleeve({ cover, title }: { cover?: NonNullable<WorkspaceAppDataItem["media"]>[number]; title: string }) {
  const [hue, accent] = titleHues(title);
  const art = cover ? <PackCover documentId={cover.document_id} label={title} />
    : <span className="workspace-app-data__record-art" style={{ "--record-hue": hue, "--record-accent": accent } as CSSProperties} />;
  return <span className="workspace-app-data__record-stage" aria-hidden="true">
    <span className="workspace-app-data__record-disc"><span className="workspace-app-data__record-label">{art}</span></span>
    <span className="workspace-app-data__record-sleeve">{art}</span>
  </span>;
}

/** Whether a result is still being made, so its card shows that it is working. */
function beingMade(status?: string | null): boolean {
  return status === "running" || status === "queued" || status === "needs_you";
}

function GalleryItems({ items }: { items: WorkspaceAppDataItem[] }) {
  const openRun = useContext(RunOpenerContext);
  if (!items.length) return null;
  const records = items.some((item) => String(item.media?.[0]?.mime_type || "").toLowerCase().startsWith("audio/"));
  return <div className={`workspace-app-data__packs workspace-app-data__gallery${records ? " workspace-app-data__gallery--records" : ""}`}>{items.map((item) => {
    const media = item.media?.[0];
    const mime = String(media?.mime_type || "").toLowerCase();
    // A track is a record: a sleeve with the disc behind it, which slides out and turns on hover.
    if (mime.startsWith("audio/")) {
      const run = item.run_id && item.workflow_id ? { id: item.run_id, workflow_id: item.workflow_id, binding_id: item.binding_id } : null;
      const title = item.title || t("component.workspace_app_view.data_untitled");
      const cover = item.media?.find((entry) => String(entry.mime_type || "").toLowerCase().startsWith("image/"));
      const detail = item.fields?.map((field) => field.value).find((value) => typeof value === "string" && value.trim());
      return <button key={item.id} type="button" className="workspace-app-data__record-card" disabled={!run || !openRun}
        data-making={beingMade(item.status) || undefined} onClick={() => run && openRun?.(run)}>
        <RecordSleeve cover={cover} title={title} />
        {beingMade(item.status) && typeof item.progress_percent === "number"
          && <span className="workspace-app-data__record-progress"><MakingMark percent={item.progress_percent} /></span>}
        <span className="workspace-app-data__record-caption">
          <strong title={title}>{title}</strong>
          {typeof detail === "string" && <small title={detail}>{detail}</small>}
          {item.status && item.status !== "completed" && <small>{t(`component.workflow_run.state.${item.status}`)}</small>}
        </span>
      </button>;
    }
    const pictured = Boolean(media) && (mime.startsWith("image/") || mime.startsWith("video/"));
    const run = item.run_id && item.workflow_id ? { id: item.run_id, workflow_id: item.workflow_id, binding_id: item.binding_id } : null;
    const title = item.title || t("component.workspace_app_view.data_untitled");
    return <button key={item.id} type="button" className="workspace-app-data__pack" disabled={!run || !openRun}
      onClick={() => run && openRun?.(run)}>
      <span className="workspace-app-data__pack-cover">
        {pictured ? <PackCover documentId={media!.document_id} label={title} video={mime.startsWith("video/")} />
          : !media && beingMade(item.status) ? <MakingCover title={title} percent={item.progress_percent} />
          : <span className="workspace-app-data__gallery-tile" data-kind={media ? mime.split("/")[0] || "file" : "pending"}>
            {mime.startsWith("audio/") ? <IconMusicNote size={22} /> : media ? <IconDocument size={22} /> : null}
          </span>}
        {media && (mime.startsWith("audio/") || mime.startsWith("video/")) && <span className="workspace-app-data__gallery-play" aria-hidden="true"><IconPlay size={12} /></span>}
      </span>
      <span className="workspace-app-data__pack-caption">
        <strong title={title}>{title}</strong>
        {item.status && item.status !== "completed" && <small>{t(`component.workflow_run.state.${item.status}`)}</small>}
      </span>
      {item.fields?.length ? <AppDataFields fields={item.fields} dates={[]} className="workspace-app-data__fields" humanizeText /> : null}
    </button>;
  })}</div>;
}

function PackItems({ items, stageLabels, itemAction, planAction }: { items: WorkspaceAppDataItem[]; stageLabels?: Record<string, string>; itemAction?: BlockConfig["item_action"]; planAction?: BlockConfig["plan_action"] }) {
  const packs = packsOf(items, stageLabels);
  const openRun = useContext(RunOpenerContext);
  const actions = useContext(ActionOpenerContext);
  // A started child opens with its project; a planned one, which has none
  // yet, opens with its parent and plan code.
  const actionFor = (pack: Pack): PackAction | undefined => {
    if (pack.id.startsWith("plan:")) {
      if (!planAction || !actions?.has(planAction.action)) return undefined;
      const values = Object.fromEntries(Object.entries(planAction.inputs)
        .map(([key, from]) => [key, (from === "parent" ? pack.parentId : pack.code) || ""]));
      return Object.values(values).every(Boolean)
        ? { action: planAction.action, label: planAction.label, values, planned: true } : undefined;
    }
    if (!itemAction || !actions?.has(itemAction.action)) return undefined;
    if (itemAction.stages?.length && !itemAction.stages.includes(pack.stageKey || "")) return undefined;
    return { action: itemAction.action, label: itemAction.label, values: { [itemAction.input]: pack.id }, planned: false };
  };
  if (!packs.length) return null;
  return <div className="workspace-app-data__packs">{packs.map((pack) => (
    <button key={pack.id} type="button" className="workspace-app-data__pack"
      onClick={(event) => openDetail({
        key: `pack:${pack.id}`, title: pack.label, subtitle: pack.stage, width: 520,
        body: <PackDetail pack={pack} openRun={openRun} action={actionFor(pack)}
          onAction={(action) => { closeDetail(); actions?.open(action.action, action.values); }} />,
        returnFocusTo: event.currentTarget,
      })}>
      <span className="workspace-app-data__pack-cover"><PackCover documentId={pack.cover?.document_id} label={pack.label} video={pack.coverIsVideo} /></span>
      <span className="workspace-app-data__pack-caption">
        <strong title={pack.label}>{pack.label}</strong>
        {pack.stage && <small>{pack.stage}</small>}
      </span>
    </button>
  ))}</div>;
}

/** What a project keeps for everything made in it: each role's voice or named image, and where it is held. */
function ReferenceItems({ items }: { items: WorkspaceAppDataItem[] }) {
  if (!items.length) return null;
  return <ul className="workspace-app-data__references">{items.map((item) => <li key={item.id || item.role}>
    {item.media?.length
      ? <span className="workspace-app-data__reference-media"><RecordMedia media={item.media.slice(0, 1)} /></span>
      : <span className="workspace-app-data__item-icon" aria-hidden="true"><IconMicrophone size={16} /></span>}
    <span className="workspace-app-data__item-copy">
      <strong>{item.reference?.voice || humanize(item.role || "")}</strong>
      <small>{[item.reference?.voice ? humanize(item.role || "") : null, item.reference?.model].filter(Boolean).join(" · ")}</small>
    </span>
    {item.inherited && <span className="workspace-app-data__status">
      {t("component.workspace_app_view.data_reference_inherited", { name: item.holder_label || t("component.workspace_app_view.data_reference_parent") })}
    </span>}
  </li>)}</ul>;
}

function Block({ workspaceId, block, config, title, hideTitle = false, showRollupNext = false, compactDocuments = false, onMore, loadingMore }: { workspaceId: string; block: WorkspaceAppDataBlockResult; config?: WorkspaceAppDataComponent["blocks"][number]; title?: string | null; hideTitle?: boolean; showRollupNext?: boolean; compactDocuments?: boolean; onMore: (block: WorkspaceAppDataBlockResult) => void; loadingMore: boolean }) {
  const blockTitle = title || block.title || block.summary || humanize(block.key);
  const body = block.type === "summary" ? <SummaryItems items={block.items} /> : block.type === "metrics" ? <MetricItems items={block.items} /> : block.type === "progress" ? <ProgressItems workspaceId={workspaceId} items={block.items} stageLabels={config?.stage_labels} /> : block.type === "project_rollup" ? <RollupItems workspaceId={workspaceId} items={block.items} showNext={showRollupNext} completion={config?.completion} stageLabels={config?.stage_labels} emptyMessage={config?.empty_message} /> : block.type === "references" ? <ReferenceItems items={block.items} /> : block.type === "gallery" ? <GalleryItems items={block.items} /> : block.type === "packs" ? <PackItems items={block.items} stageLabels={config?.stage_labels} itemAction={config?.item_action} planAction={config?.plan_action} /> : block.type === "documents" && block.items.some((item) => item.folder !== undefined) ? <FolderItems workspaceId={workspaceId} items={block.items} blockTitle={blockTitle} compactDocuments={compactDocuments} /> : <RecordItems workspaceId={workspaceId} items={block.items} documents={block.type === "documents"} blockTitle={blockTitle} compactDocuments={compactDocuments} />;
  const showTitle = !hideTitle && !(block.type === "documents" && block.items.length === 1 && !block.error && blockTitle);
  const compactRollup = block.type === "project_rollup" && !showRollupNext && block.status === "ready"
    && block.items.length === 1 && !block.error && !block.next_cursor;
  const compactDocument = block.type === "documents" && config?.limit === 1 && block.items.length === 1 && !block.error && !block.next_cursor;
  return <section className={`workspace-app-data__block workspace-app-data__block--${block.type}${compactRollup ? " is-compact-rollup" : ""}${config?.completion ? " is-completion-block" : ""}${compactDocument ? " is-compact-document" : ""}`} aria-label={blockTitle}>
    {(showTitle || block.status === "stale") && <header>{showTitle && <h4>{blockTitle}</h4>}{block.status === "stale" && <span className="workspace-app-data__stale">{t("component.workspace_app_view.data_stale")}</span>}</header>}
    {block.error || block.status === "error" ? <p className="workspace-app-data__error">{block.error?.message || t("component.workspace_app_view.data_error")}</p> : block.status === "empty" && !block.items.length && config?.empty_message ? <p className="workspace-app-data__empty">{config.empty_message}</p> : body}
    {block.next_cursor && <Button size="sm" variant="ghost" onClick={() => onMore(block)} disabled={loadingMore}>{loadingMore ? t("component.workspace_app_view.data_loading") : t("component.workspace_app_view.data_more")}</Button>}
  </section>;
}

function DefaultWorkspaceAppDataView({ workspaceId, component, preview = false, openRun, openAction }: Props) {
  const tabId = useId();
  const contextId = useId();
  const tabRefs = useRef<Record<string, HTMLButtonElement | undefined>>({});
  const contextsQuery = useInfiniteQuery({
    queryKey: ["workspace-app-data-contexts", workspaceId, component.key, component.visibility],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ signal, pageParam }) => api.workspaces.appViewContexts(workspaceId, component.key, { limit: 50, cursor: pageParam, signal }),
    getNextPageParam: (lastPage) => lastPage.next_cursor || undefined,
    enabled: !preview && Boolean(workspaceId && component.key),
    staleTime: 20_000,
    refetchInterval: 30_000,
  });
  const contexts = useMemo(() => contextsQuery.data?.pages.flatMap((page) => page.items) ?? EMPTY_CONTEXTS, [contextsQuery.data]);
  const contextLabels = useMemo(() => {
    const counts = new Map<string, number>();
    for (const item of contexts) counts.set(item.label, (counts.get(item.label) || 0) + 1);
    const dates = contexts.map((item) => item.created_at).filter((value): value is string => Boolean(value));
    return new Map(contexts.map((item) => [item.id,
      item.kind === "project" && (counts.get(item.label) || 0) > 1 && item.created_at
        ? `${item.label} · ${formatDateCompact(item.created_at, dates)}` : item.label,
    ]));
  }, [contexts]);
  const [context, setContext] = useState<WorkspaceAppDataContext | null>(null);
  // In card mode the component starts as the cards; one opens that project's view.
  const cardsMode = !preview && component.context.display === "cards";
  const [opened, setOpened] = useState(false);
  const [window, setWindow] = useState("7d");
  const [extraBlocks, setExtraBlocks] = useState<Record<string, WorkspaceAppDataBlockResult>>({});
  const [loadingBlock, setLoadingBlock] = useState<string | null>(null);
  const [moreError, setMoreError] = useState(false);
  const [activeSection, setActiveSection] = useState("");
  const scope = `${workspaceId}:${component.key}:${context?.kind}:${context?.id}:${window}:${component.visibility?.block || ""}`;
  const activeScope = useRef(scope);
  activeScope.current = scope;
  useEffect(() => { setContext((current) => contexts.find((item) => item.id === current?.id && item.kind === current?.kind) || contexts[0] || null); }, [contexts]);
  useEffect(() => { setExtraBlocks({}); setLoadingBlock(null); setMoreError(false); }, [scope]);
  const dataQuery = useQuery({
    queryKey: ["workspace-app-data", workspaceId, component.key, context?.kind, context?.id, window, component.visibility],
    queryFn: ({ signal }) => api.workspaces.appViewComponentData(workspaceId, component.key, { contextKind: context!.kind, contextId: context!.id, window, signal }),
    enabled: !preview && Boolean(context) && (!cardsMode || opened),
    staleTime: 15_000,
    refetchInterval: 30_000,
  });
  const blocks = useMemo(() => (dataQuery.data?.blocks || []).map((block) => {
    const extra = extraBlocks[block.key];
    return extra ? {
      ...block,
      items: Array.from(new Map([...block.items, ...extra.items].map((item) => [item.id, item])).values()),
      next_cursor: extra.next_cursor,
      error: extra.error || block.error,
    } : block;
  }), [dataQuery.data?.blocks, extraBlocks]);
  const loadMore = async (block: WorkspaceAppDataBlockResult) => {
    if (!context || !block.next_cursor) return;
    setLoadingBlock(block.key);
    setMoreError(false);
    const requestScope = scope;
    try {
      const page = await api.workspaces.appViewComponentData(workspaceId, component.key, {
        contextKind: context.kind,
        contextId: context.id,
        block: block.key,
        cursor: block.next_cursor,
        limit: 100,
        window,
      });
      if (activeScope.current !== requestScope) return;
      const next = page.blocks.find((item) => item.key === block.key);
      if (next) setExtraBlocks((current) => ({
        ...current,
        [block.key]: { ...next, items: [...(current[block.key]?.items || []), ...next.items] },
      }));
    } catch {
      if (activeScope.current === requestScope) setMoreError(true);
    } finally {
      if (activeScope.current === requestScope) setLoadingBlock(null);
    }
  };
  const previewBlocks: WorkspaceAppDataBlockResult[] = component.blocks.map((block) => ({ ...block, status: "waiting", items: [], summary: block.title || null, next_cursor: null }));
  const shownBlocks = preview ? previewBlocks : blocks;
  const configuredBlocks = new Map(component.blocks.map((block) => [block.key, block]));
  const renderedBlocks = shownBlocks.filter((block) => preview || Boolean(block.items.length || block.error || block.next_cursor)
    || (block.status === "empty" && Boolean(configuredBlocks.get(block.key)?.empty_message))
    || block.status === "stale" || block.status === "error");
  const sections = useMemo(() => {
    const groups = component.display?.groups?.length ? component.display.groups : [{
      key: component.key, title: "", blocks: component.blocks.map((block) => block.key),
    }];
    const hidden = new Set(component.display?.hidden_blocks || []);
    const available = new Set(renderedBlocks.map((block) => block.key));
    return groups.map((group) => ({ ...group, blocks: group.blocks.filter((key) => !hidden.has(key) && available.has(key)) }))
      .filter((group) => group.blocks.length > 0);
  }, [component.key, component.blocks, component.display, renderedBlocks]);
  const displaySection = sections.find((section) => section.key === activeSection)?.key || sections[0]?.key || "";
  const tabLayout = component.display?.layout === "tabs";
  const activeSections = tabLayout ? sections.filter((section) => section.key === displaySection) : sections;
  const visibleBlockKeys = new Set(activeSections.flatMap((section) => section.blocks));
  const hasMetricResults = renderedBlocks.some((block) => visibleBlockKeys.has(block.key)
    && (block.type === "metrics" ? block.items : block.items.flatMap((item) => item.metrics || []))
      .some((metric) => Boolean(metric.updated_at) || (metric.value !== null && metric.value !== undefined && metric.value !== "")));
  const summaryOnly = component.blocks.every((block) => block.type === "summary");
  const contextHref = safeContextHref(context);
  const contextLabel = component.context.label || t("component.workspace_app_view.data_context");
  const showContextRow = !preview && !cardsMode && Boolean(context) && (contexts.length > 1 || context?.kind !== "workspace");
  const showTabs = tabLayout && sections.length > 1;
  const handleTabKeyDown = (section: string, event: KeyboardEvent<HTMLButtonElement>) => {
    if (!showTabs || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const currentIndex = sections.findIndex((item) => item.key === section);
    const nextIndex = event.key === "Home" ? 0 : event.key === "End" ? sections.length - 1 : (currentIndex + (event.key === "ArrowRight" ? 1 : -1) + sections.length) % sections.length;
    const next = sections[nextIndex].key;
    setActiveSection(next);
    tabRefs.current[next]?.focus();
  };
  // Keep the queries mounted so newly available records appear automatically.
  // An unavailable API must remain distinguishable from an empty source.
  if (!preview && component.visibility && !contextsQuery.isError && !dataQuery.isError
    && (!contexts.length || !context || dataQuery.isPending || dataQuery.data?.visible === false)) return null;
  if (!preview && summaryOnly && !contextsQuery.isLoading && !contextsQuery.isError && !contexts.length) return null;
  const content = <div
    id={`${tabId}-${displaySection}-panel`}
    className="workspace-app-data__content"
    role={showTabs ? "tabpanel" : undefined}
    aria-labelledby={showTabs ? `${tabId}-${displaySection}` : undefined}
  >
    {activeSections.map((section) => <div className="workspace-app-data__group" key={section.key}>
      {!showTabs && section.title && <h4 className="workspace-app-data__group-title">{section.title}</h4>}
      {section.blocks.map((key) => {
        const block = renderedBlocks.find((item) => item.key === key);
        if (!block) return null;
        const configuredBlock = configuredBlocks.get(block.key);
        const hidesDuplicateGroupTitle = !tabLayout
          && sameDisplayLabel(section.title, configuredBlock?.title);
        return <Block
          key={block.key}
          workspaceId={workspaceId}
          block={block}
          config={configuredBlocks.get(block.key)}
          title={configuredBlock?.title}
          hideTitle={(block.type === "records" && Boolean(configuredBlock?.metrics_source)) || hidesDuplicateGroupTitle
            // A lone untitled block is already named by its component.
            || (component.blocks.length === 1 && !configuredBlock?.title)}
          showRollupNext={Boolean(configuredBlock?.first)}
          onMore={preview ? () => undefined : loadMore}
          loadingMore={!preview && loadingBlock === block.key}
        />;
      })}
    </div>)}
  </div>;
  // What Flows made shows once something has been made; an empty shelf is not shown.
  const galleryOnly = component.blocks.every((block) => block.type === "gallery");
  if (galleryOnly && !preview && dataQuery.isSuccess && !activeSections.length) return null;
  return <RunOpenerContext.Provider value={openRun || null}><ActionOpenerContext.Provider value={openAction || null}><section className="workspace-app-data" aria-label={component.title || component.key}>
    <header className="workspace-app-data__heading">
      <h3>{component.title || humanize(component.key)}</h3>
      {component.description && <OneLineText as="p" text={component.description} />}
    </header>
    {(showContextRow || (!preview && hasMetricResults)) && <div className="workspace-app-data__filters">
    {showContextRow && <div className="workspace-app-data__context-row">
      {!sameDisplayLabel(component.title, contextLabel) && <label htmlFor={contexts.length > 1 ? contextId : undefined}>{contextLabel}</label>}
      <div className="workspace-app-data__context-controls">
        {contexts.length > 1 ? <select
          id={contextId}
          aria-label={contextLabel}
          title={context ? context.created_at ? `${context.label} · ${formatDateLong(context.created_at)}` : context.label : undefined}
          value={context?.id || ""}
          onChange={(event) => setContext(contexts.find((item) => item.id === event.target.value) || null)}
        >{contexts.map((item) => <option key={item.id} value={item.id} title={item.created_at ? `${item.label} · ${formatDateLong(item.created_at)}` : undefined}>{contextLabels.get(item.id)}</option>)}</select> : <span className="workspace-app-data__context-value">{context?.label}</span>}
        {contextsQuery.hasNextPage && <Button
          size="sm"
          variant="ghost"
          disabled={contextsQuery.isFetchingNextPage}
          onClick={() => void contextsQuery.fetchNextPage()}
        >{t("component.workspace_app_view.data_more")}</Button>}
        {contextHref && <a href={contextHref}>{t("component.workspace_app_view.data_open_context")} <IconArrowRight size={13} /></a>}
      </div>
    </div>}
    {!preview && hasMetricResults && <select
      className="workspace-app-data__period"
      aria-label={t("component.workspace_app_view.data_comparison_period")}
      value={window}
      onChange={(event) => setWindow(event.target.value)}
    >
        {["7d", "30d", "90d"].map((value) => <option key={value} value={value}>{t(`component.workspace_app_view.data_window_${value}`)}</option>)}
    </select>}
    </div>}
    {preview && <p className="workspace-app-data__preview">{t("component.workspace_app_view.data_preview")}</p>}
    {showTabs && <div className="workspace-app-data__toolbar">
      {showTabs && <div className="workspace-app-data__tabs" role="tablist" aria-label={component.title || component.key}>
        {sections.map((section) => <button
          key={section.key}
          ref={(node) => { tabRefs.current[section.key] = node || undefined; }}
          id={`${tabId}-${section.key}`}
          type="button"
          role="tab"
          aria-selected={displaySection === section.key}
          aria-controls={`${tabId}-${section.key}-panel`}
          tabIndex={displaySection === section.key ? 0 : -1}
          onClick={() => setActiveSection(section.key)}
          onKeyDown={(event) => handleTabKeyDown(section.key, event)}
        >{section.title}</button>)}
      </div>}
    </div>}
    {cardsMode && opened && context && <div className="workspace-app-data__opened">
      <Button size="sm" variant="ghost" onClick={() => setOpened(false)}>
        ← {t("component.workspace_app_view.data_all_contexts", { name: contextLabel })}
      </Button>
      <h4>{context.label}</h4>
    </div>}
    {cardsMode && !opened
      ? contextsQuery.isLoading ? <p className="workspace-app-data__loading"><IconRefresh size={15} aria-hidden="true" /> {t("component.workspace_app_view.data_loading")}</p>
        : contextsQuery.isError ? <p className="workspace-app-data__error">{t("component.workspace_app_view.data_error")}</p>
          : contexts.length ? <ContextCards contexts={contexts} onOpen={(item) => { setContext(item); setOpened(true); }} />
            : <span className="workspace-app-data__status workspace-app-data__awaiting">{t("component.workspace_app_view.data_awaiting")}</span>
      : preview ? content
      : contextsQuery.isLoading || (context && dataQuery.isLoading) ? <p className="workspace-app-data__loading"><IconRefresh size={15} aria-hidden="true" /> {t("component.workspace_app_view.data_loading")}</p>
        : contextsQuery.isError || dataQuery.isError ? <p className="workspace-app-data__error">{t("component.workspace_app_view.data_error")}</p>
          : activeSections.length ? content
            : <span className="workspace-app-data__status workspace-app-data__awaiting">{t("component.workspace_app_view.data_awaiting")}</span>}
    {moreError && <p className="workspace-app-data__error">{t("component.workspace_app_view.data_error")}</p>}
  </section></ActionOpenerContext.Provider></RunOpenerContext.Provider>;
}

function CalendarItemDetails({ workspaceId, item, records }: { workspaceId: string; item: WorkspaceAppDataItem; records: WorkspaceAppDataItem[] }) {
  const fieldKey = (field: NonNullable<WorkspaceAppDataItem["fields"]>[number]) => JSON.stringify([field.label, field.value, field.format, field.href, field.links, field.provider, field.secondary]);
  const seenFields = new Set((item.fields || []).map(fieldKey));
  const provider = item.fields?.find((field) => field.format === "provider");
  const fields = [
    ...(item.fields || []).filter((field) => field !== provider && !(field.format === "date" && field.value === item.calendar_at)),
    ...records.flatMap((record) => record.fields || []).filter((field) => {
      const key = fieldKey(field);
      if (seenFields.has(key)) return false;
      seenFields.add(key);
      return true;
    }),
  ];
  const dates = [item.updated_at, item.last_run_at, item.next_run_at, item.stop_after, ...(item.fields || []).map((field) => field.value)]
    .filter((value): value is string => typeof value === "string");
  return <>
    {fields.length ? <AppDataFields fields={fields} dates={dates} className="workspace-app-data__progress-fields" /> : null}
    {records.map((record) => {
      const imageMedia = record.media?.filter((media) => String(media.mime_type || "").toLowerCase().startsWith("image/")) || [];
      const fileMedia = record.media?.filter((media) => !String(media.mime_type || "").toLowerCase().startsWith("image/")) || [];
      return <div key={record.id} className="workspace-app-calendar__publication-details">
        {record.title && record.title !== (item.title || item.label) && record.title !== record.href && <span>{record.title}</span>}
        <div className="workspace-app-calendar__publication-meta">
          {safeHref(record) && <ItemLink item={record} className="workspace-app-calendar__publication-link">{t("component.publication_receipt.open_publication")}</ItemLink>}
          {record.published_at && <span>{t("component.workspace_app_view.data_published")} <time dateTime={record.published_at} title={formatDateLong(record.published_at)}>{formatDateCompact(record.published_at, dates)}</time></span>}
        </div>
        {record.tracking_status && record.tracking_status !== "configured" && Boolean(record.metrics?.length) && <span className="workspace-app-data__tracking-notice">{t(`component.workspace_app_view.data_tracking_${record.tracking_status}`)}</span>}
        {record.metrics?.length ? <MetricItems items={record.metrics} linked={false} presentation="details" /> : null}
        {imageMedia.length > 0 && <RecordMedia media={imageMedia} />}
        <RecordFiles media={fileMedia} />
        {record.execution && record.execution.run_id !== item.execution?.run_id && <ExecutionDetails workspaceId={workspaceId} execution={record.execution} labelKey="latest_related_run" />}
      </div>;
    })}
    {item.execution && <ExecutionDetails workspaceId={workspaceId} execution={item.execution} detail={item.detail} answeredElsewhere />}
  </>;
}

function CalendarContextDetails({ workspaceId, context }: { workspaceId: string; context: WorkspaceAppDataContext }) {
  const href = safeContextHref(context);
  const execution = (context as WorkspaceAppDataContext & { execution?: WorkspaceAppDataItem["execution"] }).execution;
  if (!href && !execution) return null;
  return <div className="workspace-app-data__context-controls">
    {href && <a href={href}>{t("component.workspace_app_view.data_open_context")} <IconArrowRight size={13} /></a>}
    {execution && <ExecutionDetails workspaceId={workspaceId} execution={execution} />}
  </div>;
}

export default function WorkspaceAppDataView(props: Props) {
  const { workspaceId, component, preview = false, onFocusChange } = props;
  if (!component.display?.calendar) return <DefaultWorkspaceAppDataView {...props} />;
  return <RunOpenerContext.Provider value={props.openRun || null}><WorkspaceAppCalendarView
    workspaceId={workspaceId}
    component={component}
    preview={preview}
    openRun={props.openRun}
    renderBlock={(block, config, onMore, loadingMore) => <Block
      workspaceId={workspaceId}
      block={block}
      config={config}
      title={config?.title}
      hideTitle={block.type === "records" && Boolean(config?.metrics_source) && Boolean(component.display?.groups?.some((group) => group.title && group.blocks.includes(block.key)))}
      compactDocuments
      onMore={onMore}
      loadingMore={loadingMore}
    />}
    renderItemDetails={(item, records) => <CalendarItemDetails workspaceId={workspaceId} item={item} records={records} />}
    renderItemMetrics={(records) => records.length ? records.map((record) => <div key={record.id}>
      {record.conflict && <p className="workspace-app-data__error">{t("component.workspace_app_view.data_publication_conflict", { detail: record.conflict })}</p>}
      {record.metrics?.length ? <MetricItems items={record.metrics} linked={false} presentation="compact" /> : <span className="workspace-app-data__tracking-notice">{t(`component.workspace_app_view.data_tracking_${record.tracking_status || "pending"}`)}</span>}
    </div>) : <span className="workspace-app-data__tracking-notice">{t("component.workspace_app_view.data_empty")}</span>}
    renderContextDetails={(context) => <CalendarContextDetails workspaceId={workspaceId} context={context} />}
    renderItemResponse={(item) => <CalendarItemResponse workspaceId={workspaceId} item={item} />}
    onFocusChange={onFocusChange}
  /></RunOpenerContext.Provider>;
}
