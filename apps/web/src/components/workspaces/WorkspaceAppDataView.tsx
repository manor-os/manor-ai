import { createContext, useCallback, useContext, useEffect, useId, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent, type ReactNode } from "react";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";

import { ApiError, api, translateApiError, type WorkspaceAppDataBlockResult, type WorkspaceAppDataComponent, type WorkspaceAppDataContext, type WorkspaceAppDataContextsResponse, type WorkspaceAppCardAction, type WorkspaceAppDataItem, type WorkspaceAppDataMetric, type WorkspaceAppDataResponse, type WorkspaceAppPresentedField, type WorkspaceAppSummaryField } from "../../lib/api";
import { useStoredQueryData } from "../../lib/appViewSnapshots";
import { displayReadInterval, pagedReadInterval } from "../../lib/readRefreshPolicy";
import { useElementVisible } from "../../lib/useElementVisible";
import { usePageVisible } from "../../lib/usePageVisible";
import { formatDateCompact, formatDateLong, getPreferredTimeZone, relativeTime } from "../../lib/format";
import { getLocale, t } from "../../lib/i18n";
import { formatWorkspaceStatValue, workspaceStatAttentionPresentation } from "../../lib/workspaceStats";
import { IconArrowDown, IconArrowRight, IconArrowUp, IconCheck, IconCopy, IconSearch, IconDocument, IconDownload, IconFlow, IconFolder, IconMicrophone, IconMusicNote, IconPlay, IconPlus, IconRefresh, IconSort } from "../icons";
import IntegrationLogo from "../IntegrationLogo";
import Button from "../ui/Button";
import Chip from "../ui/Chip";
import Select from "../ui/Select";
import ConfirmDialog from "../ui/ConfirmDialog";
import CompactCard from "../ui/CompactCard";
import InlineDocumentText, { inlineDocumentTextKind } from "../InlineDocumentText";
import WorkflowRunRetryControl from "../workflows/WorkflowRunRetryControl";
import WorkflowRunResumeControl from "../workflows/WorkflowRunResumeControl";
import { ReviewPlayer, ReviewText } from "../workflows/ReviewDeliverables";
import { artifactKind, type ReviewArtifactReference } from "../workflows/WorkflowApprovalReview";
import { workflowRetrySchemaIsCompatible, workflowRunBackgroundWait, type WorkflowHistoryRun } from "../workflows/workflowRunDisplay";
import WorkspaceAppCalendarView from "./WorkspaceAppCalendarView";
import PostPreviewItems from "./WorkspaceAppPostPreview";
import { MakingCover } from "./WorkspaceAppMakingMark";
import { closeDetail, openDetail } from "../../stores/detail";
import { makingLabel, titleHues } from "./workspaceAppRunDisplay";
import { CALENDAR_LATE_PUBLISH_CHOICE, CALENDAR_LATE_PUBLISH_VARIABLE, calendarCanContinuePublication, calendarItemOverdue, calendarOffersLatePublish } from "./workspaceAppCalendar";
import { loadWaitingRun } from "../workflows/workflowWaitingRun";
import OneLineText from "../ui/OneLineText";
import { SignedMediaRefreshContext, useSignedMediaRefresh, useSignedMediaUrl } from "../ui/useSignedMediaUrl";
import { useWorkspaceAppDraftId } from "./workspaceAppDraft";
import { aggregate, barShare, boardColumns, csvFileName, groupItems, isStringList, keepListed, readStored, sortTableItems, tableSortValue, toCsv, toggleSelection, writeStored } from "./workspaceAppCollection";
import { emailHref, fieldCopyText, fieldNumberText, fieldPlainText, formatDateRange, formatRelativeTime, mapsHref, phoneHref, progressShare, safeColor } from "./workspaceAppFieldFormat";
import WorkspaceAppChart, { Sparkline } from "./WorkspaceAppChart";
import { ColumnsMenu, GanttView, PlacesView, TimelineView, TreeView, type RowPresenter } from "./WorkspaceAppCollectionViews";
import { ExportButton, GroupPicker, SavedViewTabs, SelectionBar, downloadCsv, type ListState } from "./WorkspaceAppListTools";
import { useWindowedRows } from "./useWindowedRows";
import { useWorkspaceAppSelection } from "../../stores/workspaceAppSelection";
import Checkbox from "../ui/Checkbox";
import InfoPopover from "../ui/InfoPopover";
import UserAvatar from "../ui/UserAvatar";
import AudioWaveform from "../ui/AudioWaveform";
import ImagePreview from "../file-preview/ImagePreview";
import { ClipsField, DownloadAllButton, ImageCompareField, MediaCarousel, MediaFieldPlayer, MediaFrame, MediaPlayer, MediaViewer, PackCover, TimelineField, mediaKind, versionLabel, type MediaViewerEntry } from "./WorkspaceAppMedia";
import { aspectRatioCss, mediaGroups } from "./workspaceAppMediaPresentation";
import { copyText } from "../../lib/clipboard";
import { useToastStore } from "../../stores/toast";
import "./WorkspaceAppDataView.css";
import { PageLoading } from "../ui/Skeleton";

/** Opens a run where its progress shows: its App entry, or the run page. */
export type WorkspaceAppRunOpener = (run: { id: string; workflow_id: string; binding_id?: string | null }) => void;
/** Opens one of the App's own actions with some of its inputs already filled (a pack's Remake).
 *  ``start`` runs it in place instead, for an action whose inputs are all filled (refresh this post). */
export type WorkspaceAppActionOpener = {
  has: (actionKey: string) => boolean;
  open: (actionKey: string, values: Record<string, unknown>) => void;
  start?: (actionKey: string, values: Record<string, unknown>) => Promise<{ id: string }>;
};
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
  const signed = useSignedMediaUrl(item.thumbnail_url);
  const viaApi = !signed.src && !signed.waiting;
  const href = item.viewer_url && isSafeHref(item.viewer_url) ? item.viewer_url : null;
  const label = item.label || item.title || t("component.workspace_app_view.data_document");
  useEffect(() => {
    if (!viaApi) return undefined;
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
  }, [item.document_id, item.file_size, item.updated_at, viaApi]);

  const content = signed.src
    ? <img src={signed.src} alt={label} loading="lazy" decoding="async" onError={signed.onError} />
    : !thumbnail || failed
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
    // A clip has a frame to show; a document without one (a script, a plan) is a page.
    : <IconDocument size={14} aria-hidden="true" />;
}

/** A document on a data field, shown as what it is: a clip plays, a picture shows, written words read in place. */
function DocumentPreviewBody({ documentId, label }: { documentId: string; label?: string }) {
  const [preview, setPreview] = useState<{ url: string; type: string; blob: Blob } | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let active = true;
    let objectUrl = "";
    const load = async () => {
      try {
        const blob = await api.documents.previewBlob(documentId, { cache: true });
        // A recording often comes back typed as bytes; the document's own type says it is audio.
        const type = /^(?:audio|video|image|text)\//i.test(blob.type) ? blob.type.toLowerCase()
          : String((await api.documents.get(documentId)).mime_type || blob.type).toLowerCase();
        objectUrl = URL.createObjectURL(blob);
        if (active) setPreview({ url: objectUrl, type, blob });
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
  if (!preview) return <PageLoading label={t("component.result_files.loading_preview")} minHeight={96} />;
  const name = label || t("component.workspace_app_view.data_preview_document");
  if (preview.type.startsWith("video/")) return <video className="workspace-app-data__document-video" src={preview.url} controls playsInline preload="metadata"
    aria-label={name} onError={() => setFailed(true)} />;
  // A recording plays here (it showed "preview failed" before); its waveform is read from the same bytes.
  if (preview.type.startsWith("audio/")) return <AudioWaveform src={preview.url} data={preview.blob} label={name} onError={() => setFailed(true)} />;
  // A picture opens larger, to zoom and pan.
  if (preview.type.startsWith("image/")) return <ImagePreview src={preview.url} alt={name} imageClassName="workspace-app-data__document-video" onError={() => setFailed(true)} />;
  if (/^text\/(?:csv|tab-separated-values)/.test(preview.type)) return <InlineDocumentText documentId={documentId} kind="table" label={name} open />;
  if (preview.type.startsWith("text/")) return <InlineDocumentText documentId={documentId} kind="markdown" label={name} open />;
  return <p role="alert">{t("component.result_files.preview_failed")}</p>;
}

/** A compact card for a document on a data field; its player and the open action live in the global detail pop-up. */
function DocumentPreviewCard({ documentId, href, label }: { documentId: string; href: string; label?: string }) {
  const navigate = useNavigate();
  // The card names what the document is ("Report", "Script"); "Preview" alone said nothing.
  const title = label?.trim() || t("component.workspace_app_view.data_preview_document");
  return <span className="workspace-app-data__document-preview"><CompactCard
    icon={<span className="workspace-app-data__document-thumb"><DocumentThumbnail documentId={documentId} /></span>}
    title={title}
    ariaLabel={title}
    onClick={() => openDetail({
      title,
      width: 640,
      body: <DocumentPreviewBody documentId={documentId} label={title} />,
      primaryAction: {
        label: t("component.workspace_app_view.data_open_document"),
        onClick: () => {
          closeDetail();
          navigate(href);
        },
      },
    })}
  /></span>;
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
    // An entry of a list (a decision in a history) has no name of its own; its fields say what it is.
    const unnamed = !documents && !item.title && !item.label && Boolean(item.fields?.length);
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
        {!unnamed && <strong title={primaryTitle}>{primary}</strong>}{secondary && !hasMetrics && !hasPlatform && <small title={secondary}>{secondary}</small>}
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
      const trend = (item.trend || []).map((point) => point.value).filter((value): value is number => typeof value === "number");
      const content = <>
        <span className="workspace-app-data__metric-copy">
          <small title={metricLabel(item)}>{metricLabel(item)}</small>
          {!detailsOnly && <strong title={valueHint || undefined} tabIndex={valueHint ? 0 : undefined}
            aria-description={valueHint ? `${metricLabel(item)}: ${metricValue(item)}. ${valueHint}` : undefined}>{metricValue(item)}</strong>}
          {presentation === "full" && trend.length > 1 && <Sparkline points={trend} label={t("component.workspace_app_view.metric_trend")} />}
          {!compact && showMeta && <span className="workspace-app-data__metric-meta">
            {hasChange && <b className={change > 0 ? "is-positive" : change < 0 ? "is-negative" : undefined}>{change > 0 ? "+" : ""}{change.toLocaleString()}</b>}
            {visibleAttention && <em className={visibleAttention.type === "danger" ? "workspace-app-data__error" : "workspace-app-data__stale"}>{visibleAttention.label}</em>}
            {linked && !sharedSubtitle && item.subtitle && <PlatformLabel value={item.subtitle} />}
            {visibleError && <span className="workspace-app-data__error">{visibleError}</span>}
            {!detailsOnly && !sharedObservation && item.updated_at && <time dateTime={item.updated_at} title={formatDateLong(item.updated_at)}>{t("component.workspace_app_view.data_latest_collection")} {relativeTime(item.updated_at)}</time>}
          </span>}
        </span>
      </>;
      // How the number is worked out, from its Stat's definition; beside the link, never inside it.
      const definition = item.description ? <span className="workspace-app-data__metric-definition">
        <InfoPopover ariaLabel={t("component.workspace_app_view.metric_definition", { name: metricLabel(item) })} width={280} size={14} align="left">
          <p className="workspace-app-data__metric-definition-text">{item.description}</p>
          {item.window && <p className="workspace-app-data__metric-definition-text is-muted">{t("component.workspace_app_view.metric_window", { window: humanize(item.window) })}</p>}
        </InfoPopover></span> : null;
      const key = item.id || `${metricLabel(item)}:${index}`;
      if (linked && safeHref(item) && definition) {
        return <div key={key} className="workspace-app-data__metric-wrap"><ItemLink item={item} className="workspace-app-data__metric">{content}</ItemLink>{definition}</div>;
      }
      return linked ? <ItemLink key={key} item={item} className="workspace-app-data__metric">{content}{definition}</ItemLink>
        : <div key={key} className="workspace-app-data__metric">{content}{definition}</div>;
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
  const visible = usePageVisible();
  const controlQuery = useQuery({
    queryKey: ["workflow-run-history-control", runId],
    queryFn: ({ signal }) => api.workflows.getRun(runId, false, signal),
    enabled: visible && Boolean(workspaceId && runId) && enabled,
    staleTime: 15_000,
    // Events are hints; retain recovery after a missed event or initial read failure.
    refetchInterval: (query) => displayReadInterval(query.state, 60_000),
    retry: false,
    refetchOnWindowFocus: "always",
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
  const row = useElementVisible<HTMLSpanElement>();
  const runId = item.execution?.run_id || "";
  const paused = String(item.execution?.status || "").toLowerCase() === "paused";
  const { action, responseAllowed } = useRunControl(workspaceId, runId, paused && row.visible);
  return <span ref={row.ref}>{paused && responseAllowed && <WorkflowRunResumeControl runId={runId} variant="primary"
    label={t(action?.kind === "workflow_approval" ? "component.workspace_app_view.calendar_review" : "component.workspace_app_view.calendar_respond")} />}</span>;
}

function ExecutionRecovery({ workspaceId, runId, href, answeredElsewhere = false }: {
  workspaceId: string; runId: string; href: string | null;
  /** The response already sits beside this run (a calendar post's row), so it is not offered twice. */
  answeredElsewhere?: boolean;
}) {
  const queryClient = useQueryClient();
  // The attempt that the retry started; it replaces the Retry control until the view moves to that run.
  const [retryStartedRunId, setRetryStartedRunId] = useState<string | null>(null);
  const row = useElementVisible<HTMLDivElement>();
  const { controlQuery, current, action, responseAllowed } = useRunControl(workspaceId, runId, !retryStartedRunId && row.visible);
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
  return <div ref={row.ref} className="workspace-app-data__recovery">
    {controlQuery.isError && <span>{t("component.workspace_app_view.data_recovery_load_error")}</span>}
    <div className="workspace-app-data__recovery-actions">
      {responseAllowed && !answeredElsewhere && <WorkflowRunResumeControl runId={runId} />}
      {retryStartedRunId ? <span role="status">{t("component.workspace_app_view.data_retry_started")} <span className="mono">{retryStartedRunId}</span></span> : (retryAllowed || effectReviewAllowed || reviewRequired) && current && <WorkflowRunRetryControl
        runId={runId}
        workspaceId={workspaceId}
        bindingId={current.binding_id}
        workflowId={current.workflow_id}
        size="sm"
        reviewRequired={Boolean(effectReviewAllowed)}
        onRetried={(retried) => {
          setRetryStartedRunId(retried.id);
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

/** The step a run is on, as the workflow names it; the step id stands in only for runs whose definition is not read. */
function currentStepText(execution: NonNullable<WorkspaceAppDataItem["execution"]>): string | null {
  return execution.current_step_label || (execution.current_step_id ? humanize(execution.current_step_id) : null);
}

function ExecutionDetails({ workspaceId, execution, labelKey, detail, answeredElsewhere }: {
  workspaceId: string;
  answeredElsewhere?: boolean;
  execution: NonNullable<WorkspaceAppDataItem["execution"]>;
  labelKey?: "latest_related_run";
  detail?: string | null;
}) {
  const status = String(execution.status || "").toLowerCase();
  const currentStep = currentStepText(execution);
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
          {currentStep && <span>{currentStep}</span>}
          {detail && <span>{humanize(detail)}</span>}
        </div>}
        {execution.error && <p>{execution.error}</p>}
      </div>
    </details>
    <ExecutionRecovery key={execution.run_id} workspaceId={workspaceId} runId={execution.run_id} href={href} />
  </div>;
  if (status === "cancelled") return <div className="workspace-app-data__execution is-failed">
    <span className="workspace-app-data__error"><b>{label}</b> {stateLabel(execution.status)}</span>
    {currentStep && <span>{currentStep}</span>}
    {detail && <span>{humanize(detail)}</span>}
    {execution.error && <span className="workspace-app-data__execution-error" title={execution.error}>{execution.error}</span>}
    <ExecutionRecovery key={execution.run_id} workspaceId={workspaceId} runId={execution.run_id} href={href} />
  </div>;
  const content = <div className="workspace-app-data__execution">
    <span className={needsAttention ? "workspace-app-data__error" : statusClass(status)}><b>{label}</b> {stateLabel(execution.status)}</span>
    {currentStep && status !== "completed" && <span>{currentStep}</span>}
    {detail && <span>{humanize(detail)}</span>}
    {execution.error && (isLongText(execution.error)
      ? <LongSummaryValue value={execution.error} className="workspace-app-data__execution-error-disclosure" />
      : <span className="workspace-app-data__error workspace-app-data__execution-error" title={execution.error}>{execution.error}</span>)}
    {status === "paused"
      // A sub-flow that waits for a person is answered in that sub-flow's run.
      ? <ExecutionRecovery key={execution.waiting_run?.run_id || execution.run_id} workspaceId={workspaceId}
          runId={execution.waiting_run?.run_id || execution.run_id}
          href={execution.waiting_run?.href && isSafeHref(execution.waiting_run.href) ? execution.waiting_run.href : href}
          answeredElsewhere={answeredElsewhere} />
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

/** An item's status: the page's label for its stage, else the words its Workflow gave it, else the stage. */
function itemStatusLabel(item: { status?: string | null; status_label?: string | null }, labels?: Record<string, string>): string {
  const stage = item.status || "";
  return labels?.[stage] || item.status_label || configuredStageLabel(stage, labels);
}

/** The buttons a block puts on each record's card: its item_action, then its item_actions. */
function cardActions(config?: BlockConfig): WorkspaceAppCardAction[] {
  return [...(config?.item_action ? [config.item_action] : []), ...(config?.item_actions || [])];
}

/** What a card action starts with: the record (or, with input_from "parent", its parent project) as its
 *  input, and the fields it fills from the record. */
function cardActionValues(item: WorkspaceAppDataItem, itemAction: WorkspaceAppCardAction): Record<string, unknown> | null {
  // input_from "parent" acts on the item's parent project (a publish time's campaign), not on the item.
  const target = itemAction.input_from === "parent" ? item.parent_id : item.id;
  return target ? { ...(item.action_values?.[itemAction.action] || {}), [itemAction.input]: target } : null;
}

/** An item's own App actions (write the script for this slot, edit this dish), each offered while the item is at one of its stages.
 *  One with a confirm sentence asks first, then runs in place instead of opening the input form. */
function ItemActionButtons({ workspaceId, item, itemActions, variant = "primary" }: { workspaceId?: string; item: WorkspaceAppDataItem; itemActions: WorkspaceAppCardAction[]; variant?: "primary" | "outline" }) {
  const actions = useContext(ActionOpenerContext);
  const shown = itemActions.filter((itemAction) => cardActionValues(item, itemAction) && actions?.has(itemAction.action)
    && !(itemAction.stages?.length && !itemAction.stages.includes(item.status || "")));
  if (!shown.length) return null;
  return <span className="workspace-app-data__item-action">
    {shown.map((itemAction, index) => itemAction.confirm
      ? <ConfirmedItemAction key={itemAction.action} workspaceId={workspaceId} values={cardActionValues(item, itemAction)!}
        itemAction={itemAction} variant={index ? "outline" : variant} />
      : <Button key={itemAction.action} size="sm" variant={index ? "outline" : variant}
        onClick={() => { closeDetail(); actions!.open(itemAction.action, cardActionValues(item, itemAction)!); }}>
        {itemAction.label}
      </Button>)}
  </span>;
}

const RUN_DONE = new Set(["completed", "failed", "cancelled", "canceled"]);

/** Starts an item's action in place (no input form), spins until its run ends, then reloads the results.
 *  Without an in-place start it opens the entry instead. */
function useInPlaceItemRun(values: Record<string, unknown> | undefined, action: { action: string } | undefined, workspaceId?: string) {
  const visible = usePageVisible();
  const actions = useContext(ActionOpenerContext);
  const openRun = useContext(RunOpenerContext);
  const queryClient = useQueryClient();
  const [runId, setRunId] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [failed, setFailed] = useState(false);
  const run = useQuery({
    queryKey: ["workspace-app-item-action-run", runId],
    queryFn: ({ signal }) => api.workflows.getRun(runId!, false, signal) as Promise<WorkflowHistoryRun>,
    enabled: visible && Boolean(runId),
    refetchInterval: (query) => displayReadInterval(query.state, RUN_DONE.has(query.state.data?.status || "")
      ? false : query.state.data?.status === "paused" ? 30_000 : 5_000),
    retry: false,
    refetchOnWindowFocus: "always",
  });
  const status = run.data?.status || "";
  useEffect(() => {
    if (!runId || !RUN_DONE.has(status)) return;
    setFailed(status !== "completed");
    setRunId(null);
    // The run may have removed the project the view shows (a delete), so the list of projects is read again too.
    void queryClient.invalidateQueries({ queryKey: workspaceId ? ["workspace-app-data-contexts", workspaceId] : ["workspace-app-data-contexts"] });
    void queryClient.invalidateQueries({ queryKey: workspaceId ? ["workspace-app-data", workspaceId] : ["workspace-app-data"] });
  }, [queryClient, runId, status, workspaceId]);
  // A run that stops to ask the person (a change form, a decision) is answered on its own panel,
  // not behind a button that keeps saying it is running.
  const waiting = run.data;
  useEffect(() => {
    if (!runId || !openRun || waiting?.status !== "paused" || !waiting.workflow_id || workflowRunBackgroundWait(waiting)) return;
    setRunId(null);
    openRun({ id: runId, workflow_id: waiting.workflow_id, binding_id: waiting.binding_id });
  }, [openRun, runId, waiting]);
  const start = async () => {
    if (!action || !values) return;
    if (!actions?.start) { actions?.open(action.action, values); return; }
    setStarting(true);
    setFailed(false);
    try {
      setRunId((await actions.start(action.action, values)).id);
    } catch {
      setFailed(true);
    } finally {
      setStarting(false);
    }
  };
  return { busy: starting || Boolean(runId), failed, start };
}

/** An item action that asks first (a confirm sentence: take this dish off the menu?) and then runs in place. */
function ConfirmedItemAction({ workspaceId, values, itemAction, variant }: { workspaceId?: string; values: Record<string, unknown>; itemAction: WorkspaceAppCardAction; variant: "primary" | "outline" }) {
  const [asking, setAsking] = useState(false);
  const { busy, failed, start } = useInPlaceItemRun(values, itemAction, workspaceId);
  const label = busy ? t("component.workspace_app_view.item_action_running", { label: itemAction.label })
    : failed ? t("component.workspace_app_view.item_action_failed", { label: itemAction.label }) : itemAction.label;
  return <>
    <Button size="sm" variant={variant} disabled={busy} aria-busy={busy} onClick={() => setAsking(true)}>{label}</Button>
    <ConfirmDialog open={asking} onClose={() => setAsking(false)} onConfirm={() => { setAsking(false); void start(); }}
      title={itemAction.label} message={itemAction.confirm || ""} confirmLabel={itemAction.label} danger />
  </>;
}

/** A held post whose time has passed: publish it now by answering its publish run's late decision.
 *  Offered only while that decision is pending and offers the late choice. The run is read again on
 *  confirm, so a stale view cannot answer another decision; the publish steps still run their own checks. */
function PublishLateNow({ workspaceId, item }: { workspaceId: string; item: WorkspaceAppDataItem }) {
  const queryClient = useQueryClient();
  const row = useElementVisible<HTMLSpanElement>();
  const runId = item.execution?.run_id || "";
  const paused = String(item.execution?.status || "").toLowerCase() === "paused";
  const overdue = calendarItemOverdue(item, Date.now());
  const { action, responseAllowed } = useRunControl(workspaceId, runId, paused && overdue && row.visible);
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // A waiting post (ready or held, no run live) starts its publish check; a run paused at its late decision answers it.
  const waiting = calendarCanContinuePublication(item, Date.now());
  const offered = paused && overdue && responseAllowed && calendarOffersLatePublish(action);
  const continueRun = async () => {
    setBusy(true);
    setError("");
    try {
      // The post's own publish schedule (created with it) runs now; its run waits for the late decision.
      const { items } = await api.jobs.list({ workspace_id: workspaceId, include_workflows: true, limit: 200 });
      const job = items.find((candidate) => candidate.idempotency_key === `${item.id}:publish:1`);
      if (!job) throw new Error(t("component.workspace_app_view.publish_now_unavailable"));
      // The confirmed click is the late decision: the run answers its own wait with it.
      await api.jobs.runNow(job.job_id, undefined, {
        responses: { [CALENDAR_LATE_PUBLISH_VARIABLE]: CALENDAR_LATE_PUBLISH_CHOICE },
      });
      setAsking(false);
      for (const key of ["workflow-run", "workflow-runs", "workspace-workflow-runs", "workflow-run-history-control", "workspace-app-data", "workspace-app-data-contexts"]) {
        void queryClient.invalidateQueries({ queryKey: [key] });
      }
    } catch (cause) {
      setError(translateApiError(cause, t("component.workspace_app_view.publish_now_failed")));
    } finally {
      setBusy(false);
    }
  };
  const publish = async () => {
    setBusy(true);
    setError("");
    try {
      const current = await loadWaitingRun<WorkflowHistoryRun>(runId, (id, full = true) => api.workflows.getRun(id, full));
      const wait = current.resume_action;
      const variable = typeof wait?.response_variable === "string" ? wait.response_variable : "";
      if (current.status !== "paused" || !variable || !calendarOffersLatePublish(wait)) {
        throw new Error(t("component.workspace_app_view.publish_now_unavailable"));
      }
      const waitingRunId = typeof wait?.workflow_run_id === "string" && wait.workflow_run_id.trim() ? wait.workflow_run_id.trim() : current.id;
      await api.workflows.resumeRun(waitingRunId, { [variable]: CALENDAR_LATE_PUBLISH_CHOICE }, wait?.step_id);
      setAsking(false);
      for (const key of ["workflow-run", "workflow-runs", "workspace-workflow-runs", "workflow-run-history-control", "workspace-app-data", "workspace-app-data-contexts"]) {
        void queryClient.invalidateQueries({ queryKey: [key] });
      }
    } catch (cause) {
      setError(translateApiError(cause, t("component.workspace_app_view.publish_now_failed")));
    } finally {
      setBusy(false);
    }
  };
  return <span ref={row.ref} className="workspace-app-data__item-action workspace-app-data__publish-now">
    {(offered || waiting) && <>
      <Button size="sm" variant="outline" className="workspace-app-data__publish-now-button" disabled={busy} loading={busy} onClick={() => setAsking(true)}>{t("component.workspace_app_view.publish_now")}</Button>
      <small className="workspace-app-data__publish-now-hint">{t("component.workspace_app_view.publish_now_hint")}</small>
    </>}
    {error && <span role="alert" className="workspace-app-data__error">{error}</span>}
    <ConfirmDialog open={asking} onClose={() => { if (!busy) setAsking(false); }} onConfirm={() => void (offered ? publish() : continueRun())}
      title={t("component.workspace_app_view.publish_now")} message={t("component.workspace_app_view.publish_now_confirm")}
      confirmLabel={t("component.workspace_app_view.publish_now")} danger />
  </span>;
}

/** An item's action beside its results (refresh this post's counts): it runs in place, spins until
 *  its run ends, then reloads the results. Without an in-place start it opens the entry instead. */
function ItemResultsAction({ workspaceId, item, itemAction }: { workspaceId: string; item: WorkspaceAppDataItem; itemAction?: BlockConfig["result_action"] }) {
  const { busy, failed, start } = useInPlaceItemRun(item.id && itemAction ? { [itemAction.input]: item.id } : undefined, itemAction, workspaceId);
  const actions = useContext(ActionOpenerContext);
  if (!itemAction || !item.id || !actions?.has(itemAction.action)) return null;
  if (itemAction.stages?.length && !itemAction.stages.includes(item.status || "")) return null;
  const label = busy ? t("component.workspace_app_view.item_action_running", { label: itemAction.label })
    : failed ? t("component.workspace_app_view.item_action_failed", { label: itemAction.label }) : itemAction.label;
  return <button type="button" className={`workspace-app-data__results-action${busy ? " is-busy" : ""}${failed ? " is-failed" : ""}`}
    aria-label={label} title={label} aria-busy={busy} disabled={busy} data-testid={`item-results-action-${item.id}`}
    onClick={() => void start()}>
    <IconRefresh size={14} />
  </button>;
}

/** A run that stopped (failed, cancelled) names that instead of the item's stage. */
function stoppedLabelOf(item: WorkspaceAppDataItem): string | null {
  const status = item.execution?.status;
  return status === "failed" ? t("component.workspace_app_view.failed") : status === "cancelled" ? t("component.workspace_app_view.cancelled") : null;
}

/** The rows a person can tick in one list, and how ticking one (shift: a run of them) changes them. */
type SelectionControl = { selected: ReadonlySet<string>; toggle: (id: string, order?: string[]) => void; setAll: (ids: string[], on: boolean) => void };

function SelectBox({ item, selection, name }: { item: WorkspaceAppDataItem; selection?: SelectionControl; name: string }) {
  if (!selection || !item.id) return null;
  return <span className="workspace-app-data__select" data-select-id={item.id} onClickCapture={(event) => {
    // Shift-click ticks every row since the last one ticked, in the order the list shows them (grouped, sorted).
    if (!event.shiftKey) return;
    event.preventDefault();
    event.stopPropagation();
    const list = event.currentTarget.closest(".workspace-app-data__block");
    selection.toggle(item.id, Array.from(list?.querySelectorAll<HTMLElement>("[data-select-id]") || [], (element) => element.dataset.selectId || ""));
  }}>
    <Checkbox size="sm" checked={selection.selected.has(item.id)} onChange={() => selection.toggle(item.id)}
      aria-label={t("component.workspace_app_view.select_row", { name })} />
  </span>;
}

function ProgressItems({ workspaceId, items, stageLabels, itemActions, selection }: { workspaceId: string; items: WorkspaceAppDataItem[]; stageLabels?: Record<string, string>; itemActions: WorkspaceAppCardAction[]; selection?: SelectionControl }) {
  // A long list draws only the cards near the screen.
  const windowed = useWindowedRows<HTMLOListElement>(items.length, { estimate: 96 });
  if (!items.length) return null;
  const shown = windowed.enabled ? items.slice(windowed.start, windowed.end) : items;
  return <ol ref={windowed.ref} className="workspace-app-data__progress">
    {windowed.enabled && windowed.before > 0 && <li aria-hidden="true" className="workspace-app-data__spacer" style={{ height: windowed.before }} />}
    {shown.map((item, index) => <ProgressItem key={item.id || `${item.label}:${index}`}
      workspaceId={workspaceId} item={item} stageLabels={stageLabels} itemActions={itemActions} selection={selection} />)}
    {windowed.enabled && windowed.after > 0 && <li aria-hidden="true" className="workspace-app-data__spacer" style={{ height: windowed.after }} />}
  </ol>;
}

/** One item's card. On a board its column already names the stage, so the card names it only when it says more. */
function ProgressItem({ workspaceId, item, stageLabels, itemActions, column, selection }: { workspaceId: string; item: WorkspaceAppDataItem; stageLabels?: Record<string, string>; itemActions: WorkspaceAppCardAction[]; column?: string; selection?: SelectionControl }) {
  const execution = item.execution;
  const stoppedLabel = stoppedLabelOf(item);
  const statusLabel = item.status ? itemStatusLabel(item, stageLabels) : "";
  const showStatus = Boolean(stoppedLabel || (statusLabel && statusLabel !== column));
  const dates = [item.updated_at, item.last_run_at, item.next_run_at, item.stop_after, ...(item.fields || []).map((field) => field.value)]
    .filter((value): value is string => typeof value === "string");
  const name = humanize(item.label || item.title) || t("component.workspace_app_view.data_untitled");
  return <li data-window-row="" className={selection?.selected.has(item.id) ? "is-selected" : undefined}>
      <div className="workspace-app-data__progress-copy">
        <div className="workspace-app-data__progress-heading">
          <SelectBox item={item} selection={selection} name={name} />
          <strong title={item.label || item.title || undefined}>{name}</strong>
          {showStatus && <em
            className={stoppedLabel ? "workspace-app-data__status is-error" : undefined}
            title={stoppedLabel && item.status ? statusLabel : undefined}
          >{stoppedLabel || statusLabel}</em>}
        </div>
        {item.detail && !execution && !sameDisplayLabel(item.detail, item.label) && <small>{humanize(item.detail)}</small>}
        {item.fields?.length ? <AppDataFields fields={item.fields} dates={dates} className="workspace-app-data__progress-fields" /> : null}
        {item.enabled !== undefined && <span className="workspace-app-data__progress-fields">
          <span><b>{t("component.workspace_app_view.data_automation_state")}</b> {t(`component.workspace_app_view.data_automation_${item.state || (item.enabled ? "enabled" : "disabled")}`)}</span>
          {item.last_run_at && <span><b>{t("component.workspace_app_view.data_last_run")}</b> <time dateTime={item.last_run_at} title={formatDateLong(item.last_run_at)}>{formatDateCompact(item.last_run_at, dates)}</time>{item.last_status ? ` · ${stateLabel(item.last_status)}` : ""}</span>}
          {item.next_run_at && <span><b>{t("component.workspace_app_view.data_next_check")}</b> <time dateTime={item.next_run_at} title={formatDateLong(item.next_run_at)}>{formatDateCompact(item.next_run_at, dates)}</time></span>}
          {item.stop_after && <span><b>{t("component.workspace_app_view.data_schedule_ends")}</b> <time dateTime={item.stop_after} title={formatDateLong(item.stop_after)}>{formatDateCompact(item.stop_after, dates)}</time></span>}
        </span>}
        <ItemActionButtons workspaceId={workspaceId} item={item} itemActions={itemActions} />
        {execution && <ExecutionDetails workspaceId={workspaceId} execution={execution} detail={item.detail} />}
      </div>
    </li>;
}

/** The items as a board: a column per stage, each card the item's own card. Cards do not move here;
 *  a stage changes only through a Workflow (a card action). */
function ProgressBoard({ workspaceId, items, stages, stageLabels, itemActions, title, selection }: { workspaceId: string; items: WorkspaceAppDataItem[]; stages?: string[]; stageLabels?: Record<string, string>; itemActions: WorkspaceAppCardAction[]; title: string; selection?: SelectionControl }) {
  if (!items.length) return null;
  const columns = boardColumns(items, stages, Object.keys(stageLabels || {}));
  return <div className="workspace-app-data__board" role="group" aria-label={t("component.workspace_app_view.data_board", { list: title })}>
    {columns.map((column) => {
      const name = column.stage ? configuredStageLabel(column.stage, stageLabels) : t("component.workspace_app_view.data_board_no_stage");
      return <section key={column.stage} className="workspace-app-data__board-column" aria-label={name}>
        <header><h5>{name}</h5><span className="mono">{column.items.length}</span></header>
        {column.items.length > 0 && <ol className="workspace-app-data__progress workspace-app-data__board-cards">
          {column.items.map((item, index) => <ProgressItem key={item.id || `${item.label}:${index}`} workspaceId={workspaceId}
            item={item} stageLabels={stageLabels} itemActions={itemActions} column={name} selection={selection} />)}
        </ol>}
      </section>;
    })}
  </div>;
}

type TableSort = { column: string; direction: "asc" | "desc" };
type DataField = NonNullable<WorkspaceAppDataItem["fields"]>[number];
type TableColumn = { key: string; label: string; value: (item: WorkspaceAppDataItem) => number | string | null; text: (item: WorkspaceAppDataItem) => string };
type TableTotal = NonNullable<WorkspaceAppDataBlockResult["totals"]>[number];

function itemName(item: WorkspaceAppDataItem): string {
  return humanize(item.label || item.title) || t("component.workspace_app_view.data_untitled");
}

/** A table's columns: the name, where it stands, then a column per field in the block's order. A field one record
 *  leaves empty (hide_when_empty) keeps its place. */
function tableColumns(items: WorkspaceAppDataItem[], config?: BlockConfig, stageLabels?: Record<string, string>): TableColumn[] {
  const configured = (config?.fields || []).map((field) => (Array.isArray(field) ? field.join(".") : field.label));
  const seen = [...new Set(items.flatMap((item) => (item.fields || []).map((field) => field.label)))];
  const rank = (label: string) => (configured.indexOf(label) + 1 || configured.length + 1);
  const labels = seen.map((label, index) => ({ label, index })).sort((a, b) => rank(a.label) - rank(b.label) || a.index - b.index).map(({ label }) => label);
  const staged = items.some((item) => item.status || stoppedLabelOf(item));
  const fieldOf = (item: WorkspaceAppDataItem, label: string): DataField | undefined => item.fields?.find((field) => field.label === label);
  const status = (item: WorkspaceAppDataItem) => stoppedLabelOf(item) || (item.status ? itemStatusLabel(item, stageLabels) : null);
  return [
    { key: "name", label: t("component.workspace_app_view.data_table_name"), value: itemName, text: itemName },
    ...(staged ? [{ key: "status", label: t("component.workspace_app_view.data_table_status"), value: status, text: (item: WorkspaceAppDataItem) => status(item) || "" }] : []),
    ...labels.map((label) => ({ key: `field:${label}`, label, value: (item: WorkspaceAppDataItem) => tableSortValue(fieldOf(item, label)),
      text: (item: WorkspaceAppDataItem) => fieldPlainText(fieldOf(item, label), getLocale()) })),
  ];
}

/** The rows of a list as CSV, with the columns a person sees. */
function tableCsv(items: WorkspaceAppDataItem[], columns: TableColumn[]): string {
  return toCsv([columns.map((column) => column.label), ...items.map((item) => columns.map((column) => column.text(item)))]);
}

/** The items as a table: a row per item and a column per field the block shows, each read as its card reads it.
 *  A column header sorts the loaded rows; the row's own buttons sit at its end; its name opens the whole card. */
function ProgressTable({ workspaceId, items, columns, sort, onSort, itemActions, title, selection, totals, onOpen }: {
  workspaceId: string; items: WorkspaceAppDataItem[]; columns: TableColumn[]; sort: TableSort | null; onSort: (sort: TableSort) => void;
  itemActions: WorkspaceAppCardAction[]; title: string; selection?: SelectionControl; totals?: TableTotal[];
  onOpen?: (item: WorkspaceAppDataItem, from: HTMLElement) => void;
}) {
  const sorter = sort && columns.find((column) => column.key === sort.column);
  const rows = useMemo(() => sorter ? sortTableItems(items, sorter.value, sort!.direction, getLocale()) : items, [items, sorter, sort]);
  const windowed = useWindowedRows<HTMLTableElement>(rows.length, { estimate: 44 });
  if (!items.length) return null;
  const acting = itemActions.length > 0;
  const fieldOf = (item: WorkspaceAppDataItem, label: string): DataField | undefined => item.fields?.find((field) => field.label === label);
  const fieldColumns = columns.filter((column) => column.key.startsWith("field:"));
  const dates = items.flatMap((item) => (item.fields || []).filter((field) => field.format === "date").map((field) => field.value))
    .filter((value): value is string => typeof value === "string");
  // A bar in a cell is the value's share of the largest one its column holds.
  const columnValues = new Map(fieldColumns.map((column) => [column.label, items.map((item) => fieldOf(item, column.label)?.value)]));
  const shown = windowed.enabled ? rows.slice(windowed.start, windowed.end) : rows;
  const span = columns.length + (acting ? 1 : 0) + (selection ? 1 : 0);
  const listed = rows.map((item) => item.id).filter(Boolean);
  const allTicked = Boolean(selection) && listed.length > 0 && listed.every((id) => selection!.selected.has(id));
  // A column may carry several totals (its sum and its average), one under the other.
  const totalsByLabel = new Map<string, TableTotal[]>();
  for (const total of totals || []) totalsByLabel.set(total.label, [...(totalsByLabel.get(total.label) || []), total]);
  return <div className="workspace-app-data__table-scroll" role="region" tabIndex={0} aria-label={t("component.workspace_app_view.data_table", { list: title })}>
    <table ref={windowed.ref} className="workspace-app-data__table">
      <thead><tr>
        {selection && <th scope="col" className="workspace-app-data__table-select">
          <Checkbox size="sm" checked={allTicked} aria-label={t("component.workspace_app_view.select_all")}
            onChange={() => selection.setAll(listed, !allTicked)} />
        </th>}
        {columns.map((column) => {
          const direction = sort?.column === column.key ? sort.direction : null;
          return <th key={column.key} scope="col" aria-sort={direction === "asc" ? "ascending" : direction === "desc" ? "descending" : "none"}>
            <button type="button" onClick={() => onSort({ column: column.key, direction: direction === "asc" ? "desc" : "asc" })}
              title={t("component.workspace_app_view.data_table_sort", { column: column.label })}>
              <span>{column.label}</span>
              {direction === "asc" ? <IconArrowUp size={12} aria-hidden="true" /> : direction === "desc" ? <IconArrowDown size={12} aria-hidden="true" />
                : <IconSort size={12} aria-hidden="true" className="workspace-app-data__table-sort-idle" />}
            </button>
          </th>;
        })}
        {acting && <th scope="col"><span className="sr-only">{t("component.workspace_app_view.data_table_actions")}</span></th>}
      </tr></thead>
      <tbody>
        {windowed.enabled && windowed.before > 0 && <tr aria-hidden="true" className="workspace-app-data__spacer"><td colSpan={span} style={{ height: windowed.before }} /></tr>}
        {shown.map((item, index) => {
          const stoppedLabel = stoppedLabelOf(item);
          const status = String(item.execution?.status || "").toLowerCase();
          const name = itemName(item);
          return <tr key={item.id || `${item.label}:${index}`} data-window-row="" className={selection?.selected.has(item.id) ? "is-selected" : undefined}>
            {selection && <td className="workspace-app-data__table-select"><SelectBox item={item} selection={selection} name={name} /></td>}
            {columns.map((column) => {
              if (column.key === "name") return <th key="name" scope="row">
                {onOpen ? <button type="button" className="workspace-app-data__table-open" title={item.label || item.title || undefined}
                  onClick={(event) => onOpen(item, event.currentTarget)}>{name}</button>
                  : <span title={item.label || item.title || undefined}>{name}</span>}
                {/* A run that waits on the person or stopped keeps its answer and recovery in reach. */}
                {item.execution && ["failed", "paused", "cancelled"].includes(status)
                  && <ExecutionDetails workspaceId={workspaceId} execution={item.execution} detail={item.detail} />}
              </th>;
              if (column.key === "status") return <td key="status">{(stoppedLabel || item.status) && <span className={`workspace-app-data__status${stoppedLabel ? " is-error" : ` ${statusClass(item.status)}`}`}>
                {column.text(item)}</span>}</td>;
              const field = fieldOf(item, column.label);
              const share = field?.bar ? barShare(field.value, columnValues.get(column.label) || []) : null;
              return <td key={column.key} className={field && NUMERIC_FORMATS.has(field.format || "") ? "is-numeric" : undefined}>
                {!field ? <span className="workspace-app-data__table-empty">—</span>
                  : share !== null ? <span className="workspace-app-data__cell-bar"><span aria-hidden="true"><span style={{ width: `${share * 100}%` }} /></span><FieldValue field={field} dates={dates} /></span>
                    : <FieldValue field={field} dates={dates} />}
              </td>;
            })}
            {acting && <td className="workspace-app-data__table-actions"><ItemActionButtons workspaceId={workspaceId} item={item} itemActions={itemActions} variant="outline" /></td>}
          </tr>;
        })}
        {windowed.enabled && windowed.after > 0 && <tr aria-hidden="true" className="workspace-app-data__spacer"><td colSpan={span} style={{ height: windowed.after }} /></tr>}
      </tbody>
      {totals?.length ? <tfoot><tr>
        {selection && <td />}
        {columns.map((column, index) => {
          const shown = column.key.startsWith("field:") ? totalsByLabel.get(column.label) || [] : [];
          if (index === 0) return <th key={column.key} scope="row">{t("component.workspace_app_view.totals_all")}</th>;
          return <td key={column.key} className={shown.length ? "is-numeric" : undefined}>{shown.map((total) => <span key={total.agg} className="workspace-app-data__total">
            <small>{t(`component.workspace_app_view.totals_${total.agg}`)}</small><FieldValue field={total} dates={[]} /></span>)}</td>;
        })}
        {acting && <td />}
      </tr></tfoot> : null}
    </table>
  </div>;
}

/** A list's totals under its cards (a table shows them in its last row). */
function ListTotals({ totals }: { totals: TableTotal[] }) {
  return <dl className="workspace-app-data__list-totals">{totals.map((total) => <div key={`${total.label}:${total.agg}`}>
    <dt>{t(`component.workspace_app_view.totals_${total.agg}`)} · {total.label}</dt>
    <dd><FieldValue field={total} dates={[]} /></dd>
  </div>)}</dl>;
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
      ? <DocumentPreviewCard documentId={documentId} href={field.href} label={field.label} />
      : <a href={field.href}>{t("component.workspace_app_view.data_open_document")}</a>;
  }
  // A picture against another (the client's reference, the version before): a slider or the two side by side.
  if (field.format === "image" && field.compare && (field.compare.document_id || (field.compare.image_url && isSafePublicHref(field.compare.image_url)))
    && ((field.href && viewerDocumentId(field.href)) || (field.image_url && isSafePublicHref(field.image_url)))) {
    return <ImageCompareField field={field} documentId={field.href ? viewerDocumentId(field.href) || undefined : undefined} imageUrl={field.image_url || undefined} />;
  }
  // A picture field is the picture; it opens the image itself. A chosen frame shows all of it.
  if (field.format === "image" && field.href && isSafeHref(field.href) && viewerDocumentId(field.href)) {
    return <a href={field.href} className={mediaFieldClass("workspace-app-data__image-field", field)} style={mediaFieldStyle(field)} aria-label={field.label}>
      <DocumentThumbnail documentId={viewerDocumentId(field.href)} />
    </a>;
  }
  if (field.format === "image" && field.image_url && isSafePublicHref(field.image_url)) {
    return <a href={field.image_url} target="_blank" rel="noreferrer" className={mediaFieldClass("workspace-app-data__image-field", field)} style={mediaFieldStyle(field)} aria-label={field.label}>
      <img src={field.image_url} alt="" loading="lazy" referrerPolicy="no-referrer" />
    </a>;
  }
  if (field.format === "sparkline") return field.points && field.points.length > 1 ? <Sparkline points={field.points} label={field.label} /> : "—";
  if (field.format === "clips") return <ClipsField field={field} />;
  if (field.format === "timeline") return <TimelineField field={field} />;
  if (field.format === "link_list" && field.links?.length) {
    const links = field.links.filter((link) => isSafePublicHref(link.href));
    return links.length ? <span className="workspace-app-data__summary-links">{links.map((link) => <a key={link.href} href={link.href} target="_blank" rel="noreferrer">{link.label}</a>)}</span> : "—";
  }
  // The records a field names, each by its name; ones that are gone only count, never by their id.
  if (field.format === "record") {
    const records = field.records || [];
    if (!records.length && !field.unavailable) return "—";
    return <span className="workspace-app-data__summary-records">
      {records.map((record) => <Chip key={record.id} variant="slate" size="sm">{record.label || t("component.workspace_app_view.data_untitled")}</Chip>)}
      {Boolean(field.unavailable) && <span className="workspace-app-data__summary-records-unavailable">{t("component.workspace_app_view.data_record_unavailable", { count: field.unavailable! })}</span>}
    </span>;
  }
  if (field.format === "date" && typeof field.value === "string" && !Number.isNaN(Date.parse(field.value))) {
    return <time dateTime={field.value} title={formatDateLong(field.value)}>
      {(field.relative && formatRelativeTime(field.value, getLocale())) || formatDateCompact(field.value, dates)}</time>;
  }
  if (field.format === "date_range") {
    const range = formatDateRange(typeof field.value === "string" ? field.value : null, field.end, {
      locale: getLocale(), timeZone: getPreferredTimeZone(),
      from: (date) => t("component.workspace_app_view.data_range_from", { date }),
      until: (date) => t("component.workspace_app_view.data_range_until", { date }),
    });
    return range ? <time dateTime={typeof field.value === "string" ? field.value : field.end || undefined}>{range}</time> : "—";
  }
  // The people a field names, each by face and name; ones that are gone only count.
  if (field.format === "member") {
    const members = field.members || [];
    if (!members.length && !field.unavailable) return "—";
    return <span className="workspace-app-data__members">
      {members.map((member, index) => <span key={`${member.name}:${index}`} className="workspace-app-data__member">
        <UserAvatar name={member.name} avatarUrl={member.avatar_url} type={member.kind === "agent" ? "agent" : "user"} size={18} />
        <span>{member.name}</span>
      </span>)}
      {Boolean(field.unavailable) && <span className="workspace-app-data__summary-records-unavailable">{t("component.workspace_app_view.data_record_unavailable", { count: field.unavailable! })}</span>}
    </span>;
  }
  if (field.format === "tags") {
    return field.tags?.length ? <span className="workspace-app-data__summary-records">
      {field.tags.map((tag) => <Chip key={tag} variant="slate" size="sm">{tag}</Chip>)}
    </span> : "—";
  }
  // Several pictures in a strip; each opens on its own.
  if (field.format === "images") {
    const images = (field.images || []).filter((image) => image.document_id ? isSafeHref(image.href) && viewerDocumentId(image.href)
      : image.image_url && isSafePublicHref(image.image_url));
    if (!images.length) return "—";
    const itemClass = mediaFieldClass("workspace-app-data__images-item", field);
    const pictures = images.map((image, index) => image.document_id
      ? <a key={image.href} href={image.href} className={itemClass} style={mediaFieldStyle(field)} aria-label={image.label || `${field.label} ${index + 1}`}>
        <DocumentThumbnail documentId={viewerDocumentId(image.href)} /></a>
      : <a key={image.href} href={image.image_url!} target="_blank" rel="noreferrer" className={itemClass} style={mediaFieldStyle(field)} aria-label={`${field.label} ${index + 1}`}>
        <img src={image.image_url!} alt="" loading="lazy" referrerPolicy="no-referrer" /></a>);
    // One by one inside the card, or as a grid; a strip of small pictures otherwise.
    if (field.layout === "carousel") {
      return <MediaCarousel count={images.length} label={t("component.workspace_app_view.data_images", { label: field.label, count: images.length })}>{pictures}</MediaCarousel>;
    }
    return <span className={`workspace-app-data__images${field.layout === "grid" ? " is-grid" : ""}`} role="group" aria-label={t("component.workspace_app_view.data_images", { label: field.label, count: images.length })}>
      {pictures}
    </span>;
  }
  if (field.format === "audio" && field.href && isSafeHref(field.href) && viewerDocumentId(field.href)) {
    const documentId = viewerDocumentId(field.href);
    // A recording with its words or chapters plays with them; otherwise it is the track alone.
    if (field.transcript?.length || field.transcript_document_id || field.chapters?.length) {
      return <MediaFieldPlayer field={field} documentId={documentId} kind="audio" />;
    }
    return <span className="workspace-app-data__audio-field">
      <ReviewPlayer artifact={{ key: documentId, reference: documentId, documentId, kind: "audio", label: field.label }} />
    </span>;
  }
  if (field.format === "progress" && typeof field.value === "number") {
    const share = progressShare(field.value, field.max);
    const text = fieldNumberText(field, getLocale());
    return share === null || !text ? "—" : <span className="workspace-app-data__progress-bar">
      <span role="progressbar" aria-label={field.label} aria-valuemin={0} aria-valuemax={field.max!} aria-valuenow={field.value} aria-valuetext={text}>
        <span style={{ width: `${share * 100}%` }} /></span>
      <span className="mono">{text}</span>
    </span>;
  }
  if (field.format === "color") {
    const color = safeColor(field.value);
    return color ? <span className="workspace-app-data__color"><span style={{ background: color }} aria-hidden="true" /><span className="mono">{color}</span></span> : "—";
  }
  if (field.format === "code" && typeof field.value === "string") return <code className="workspace-app-data__code">{field.value}</code>;
  if ((field.format === "phone" || field.format === "email" || field.format === "address") && typeof field.value === "string") {
    const href = field.format === "phone" ? phoneHref(field.value) : field.format === "email" ? emailHref(field.value) : mapsHref(field.value, getLocale());
    if (!href) return field.value;
    return field.format === "address"
      ? <a href={href} target="_blank" rel="noreferrer" title={t("component.workspace_app_view.data_open_map")}>{field.value}</a>
      : <a href={href} className={field.format === "phone" ? "mono" : undefined}>{field.value}</a>;
  }
  if (field.format === "provider") return <ProviderValue provider={field.provider} label={typeof field.value === "string" ? field.value : null} secondary={field.secondary} />;
  if (field.format === "status") return <span className={`workspace-app-data__status ${statusClass(String(field.value || ""))}`}>{humanize(String(field.value || "—"))}</span>;
  if (field.format === "video" && field.href && isSafeHref(field.href) && viewerDocumentId(field.href)) {
    // A clip with captions, words, chapters or another take to compare plays here with them.
    if (playsInPlace(field)) return <MediaFieldPlayer field={field} documentId={viewerDocumentId(field.href)} kind="video" />;
    return <a href={field.href} className={mediaFieldClass("workspace-app-data__image-field workspace-app-data__video-field", field)} style={mediaFieldStyle(field)}
      aria-label={t("component.workspace_app_view.data_play_video", { label: field.label })}>
      <PackCover documentId={viewerDocumentId(field.href)} label={field.label} video />
      <span className="workspace-app-data__gallery-play" aria-hidden="true"><IconPlay size={12} /></span>
    </a>;
  }
  const numberText = fieldNumberText(field, getLocale());
  if (numberText !== null) return <span className="mono">{numberText}</span>;
  if (field.format === "rating" && typeof field.value === "number") {
    const stars = Math.round(field.value);
    return <span className="workspace-app-data__rating" role="img" aria-label={t("component.workspace_app_view.data_rating", { value: field.value })}>
      {"★".repeat(stars)}<span aria-hidden="true">{"★".repeat(5 - stars)}</span>
    </span>;
  }
  if (field.format === "boolean" && typeof field.value === "boolean") return t(`component.workspace_app_view.data_${field.value ? "yes" : "no"}`);
  const text = humanizeText && typeof field.value === "string" ? humanize(field.value) : String(field.value ?? "—");
  return field.secondary ? <>{text} <small className="workspace-app-data__platform-account">{field.secondary}</small></> : text;
}

/** Only plain words fold behind "Show more"; every other format has its own shape. */
function isLongSummaryField(field: NonNullable<WorkspaceAppDataItem["fields"]>[number]): boolean {
  return (field.format || "text") === "text" && typeof field.value === "string" && isLongText(field.value);
}

/** A player or a snippet takes a card's whole row. */
const WIDE_FORMATS = new Set(["audio", "code", "clips", "timeline"]);

/** Whether a media field plays (or compares) in place, so it takes the card's whole row. */
function playsInPlace(field: WorkspaceAppPresentedField): boolean {
  return Boolean(field.compare || field.captions_document_id || field.transcript?.length || field.transcript_document_id
    || field.chapters?.length || (field.format === "images" && field.layout));
}

/** A picture or clip field in its chosen frame (whole, never cropped) and over a checkerboard when it is a cut-out. */
function mediaFieldClass(base: string, field: WorkspaceAppPresentedField): string {
  return `${base}${field.aspect ? " is-framed" : ""}${field.transparent ? " is-transparent" : ""}`;
}

function mediaFieldStyle(field: WorkspaceAppPresentedField): CSSProperties | undefined {
  const ratio = field.aspect && field.aspect !== "auto" ? aspectRatioCss(field.aspect) : undefined;
  return ratio ? { aspectRatio: ratio } : field.aspect === "auto" ? { aspectRatio: "auto" } : undefined;
}

function isWideField(field: WorkspaceAppPresentedField): boolean {
  return isLongSummaryField(field) || WIDE_FORMATS.has(field.format || "") || playsInPlace(field);
}

/** Numbers line up at the end of a table cell. */
const NUMERIC_FORMATS = new Set(["number", "money", "percent", "duration", "file_size"]);

/** One field's value as its format shows it, with its copy button when the page offers one. */
function FieldValue({ field, dates, humanizeText = false }: { field: NonNullable<WorkspaceAppDataItem["fields"]>[number]; dates: string[]; humanizeText?: boolean }) {
  const value = isLongSummaryField(field) && typeof field.value === "string" ? <LongSummaryValue value={field.value} /> : summaryValue(field, dates, humanizeText);
  const text = field.copy ? fieldCopyText(field) : "";
  return text ? <span className="workspace-app-data__copyable">{value}<CopyFieldButton text={text} label={field.label} /></span> : <>{value}</>;
}

function CopyFieldButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1500);
    return () => window.clearTimeout(timer);
  }, [copied]);
  const name = t("component.workspace_app_view.data_copy", { label });
  return <button type="button" className="workspace-app-data__copy" aria-label={name} title={name}
    onClick={async (event) => {
      // A card opens its details on a click; copying stays a copy.
      event.stopPropagation();
      const toast = useToastStore.getState();
      if (await copyText(text)) {
        setCopied(true);
        toast.success(t("component.workspace_app_view.data_copied", { label }));
      } else {
        toast.error(t("component.workspace_app_view.data_copy_failed"));
      }
    }}>
    {copied ? <IconCheck size={13} aria-hidden="true" /> : <IconCopy size={13} aria-hidden="true" />}
  </button>;
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
    const long = isWideField(field);
    return <div key={field.label} className={long ? "workspace-app-data__field workspace-app-data__field--long" : "workspace-app-data__field"}>
      <b>{field.label}</b> <FieldValue field={field} dates={dates} humanizeText={humanizeText} />
    </div>;
  })}</div>;
}

function SummaryItems({ workspaceId, items, itemActions }: { workspaceId: string; items: WorkspaceAppDataItem[]; itemActions: WorkspaceAppCardAction[] }) {
  const fields = items.flatMap((item) => item.fields || []);
  const actions = itemActions.length ? items.map((item) => <ItemActionButtons key={item.id} workspaceId={workspaceId} item={item} itemActions={itemActions} />) : null;
  if (!fields.length) return actions && <>{actions}</>;
  const dates = fields.filter((field) => field.format === "date")
    .map((field) => field.value).filter((value): value is string => typeof value === "string");
  return <><dl className="workspace-app-data__summary">{fields.map((field) => {
    const long = isWideField(field);
    return <div key={field.label} className={long ? "workspace-app-data__summary-item--long" : undefined}>
      <dt>{field.label}</dt><dd><FieldValue field={field} dates={dates} /></dd>
    </div>;
  })}</dl>{actions}</>;
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
      stage: making ? makingLabel(making)
        : pack.stage ? stageLabels?.[pack.stage] || humanize(pack.stage) : null,
      run: making,
      cover: cover >= 0 ? shown[cover] : undefined,
      coverIsVideo: cover >= 0 && image < 0,
      artifacts,
    };
  });
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
      <div className="workspace-app-data__pack-detail-cover"><PackCover documentId={pack.cover.document_id} label={pack.label} thumbnailUrl={pack.cover.thumbnail_url} /></div>
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
  // Projects with no picture (articles, reports) read as cards of text, not empty covers lettered "Nor".
  const text = !contexts.some((item) => item.cover?.document_id);
  return <div className={`workspace-app-data__packs${text ? " workspace-app-data__gallery--text" : ""}`}>{contexts.map((item) => (
    <button key={`${item.kind}:${item.id}`} type="button" className="workspace-app-data__pack" onClick={() => onOpen(item)}>
      {!text && <span className="workspace-app-data__pack-cover"><PackCover documentId={item.cover?.document_id} label={item.label} thumbnailUrl={item.cover?.thumbnail_url} /></span>}
      <span className="workspace-app-data__pack-caption">
        <strong title={item.label}>{item.label}</strong>
        {item.progress ? <small>{item.progress.done} / {item.progress.total} · {item.progress.label}</small>
          : item.status_label ? <small>{item.status_label}</small> : null}
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
  const art = cover ? <PackCover documentId={cover.document_id} label={title} thumbnailUrl={cover.thumbnail_url} />
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

/** Whether work on a result is moving right now; one that waits on the person (needs_you) is still. */
function inMotion(status?: string | null): boolean {
  return status === "running" || status === "queued";
}

/** A result's written files (a report, a script): media that is not a picture, a clip or a track. */
function writtenFiles(item: WorkspaceAppDataItem) {
  return (item.media || []).filter((media) => !/^(?:image|video|audio)\//i.test(String(media.mime_type || ""))
    && inlineDocumentTextKind(media.title, null));
}

/** The versions one card opens: each file of its group (9:16, 1:1, 16:9), or each finished version of a result (v1…v3). */
function mediaEntries(members: WorkspaceAppDataItem[]): MediaViewerEntry[] {
  const first = members[0];
  if (members.length === 1 && first.versions?.length && first.media?.[0]) {
    const current = first.media[0];
    return [{ key: `${first.id}:current`, label: `v${first.version ?? first.versions.length + 1}`, file: current, updatedAt: first.updated_at },
      ...first.versions.filter((version) => version.media[0]).map((version) => ({
        key: `${first.id}:${version.run_id}`, label: `v${version.number}`, file: version.media[0], updatedAt: version.updated_at,
      }))];
  }
  return members.flatMap((member, index) => member.media?.[0]
    ? [{ key: member.id, label: versionLabel(member.media[0], `${index + 1}`), file: member.media[0], updatedAt: member.updated_at }] : []);
}

/** A gallery the page lays out itself (``media``): a grid, a masonry wall, a carousel, a strip, a mood board or a
 *  list of players; each card opens the media viewer with its versions, details, download, comments and file action. */
function MediaGalleryItems({ items, config, media }: { items: WorkspaceAppDataItem[]; config: BlockConfig; media: NonNullable<BlockConfig["media"]> }) {
  const openRun = useContext(RunOpenerContext);
  const actions = useContext(ActionOpenerContext);
  if (!items.length) return null;
  const layout = media.layout || "grid";
  const groups = media.group ? mediaGroups(items) : items.map((item) => ({ key: item.id, items: [item] }));
  const fileAction = config.item_action && actions?.has(config.item_action.action) ? config.item_action : undefined;
  const open = (members: WorkspaceAppDataItem[], at: number, target: HTMLElement) => {
    const first = members[0];
    const title = first.title || t("component.workspace_app_view.data_untitled");
    const entries = mediaEntries(members);
    const run = first.run_id && first.workflow_id ? { id: first.run_id, workflow_id: first.workflow_id, binding_id: first.binding_id } : null;
    if (!entries.length) {
      if (run && openRun) openRun(run);
      return;
    }
    openDetail({
      key: `media:${first.group || first.id}`, title, width: 720, returnFocusTo: target,
      body: <MediaViewer entries={entries} initial={at} options={media} title={title}
        action={fileAction && actions ? { action: fileAction, open: (key, values) => { closeDetail(); actions.open(key, values); } } : undefined} />,
      ...(run && openRun ? { secondaryActions: [{ label: t("component.workspace_app_view.data_review_run"), icon: <IconFlow size={16} />, onClick: () => { closeDetail(); openRun(run); } }] } : {}),
    });
  };
  const files = media.download ? items.flatMap((item) => item.media?.slice(0, 1) || []) : [];
  const cards = groups.map(({ key, items: members }) => {
    const first = members[0];
    const file = first.media?.[0];
    const title = first.title || t("component.workspace_app_view.data_untitled");
    const kind = mediaKind(file?.mime_type);
    const versions = members.length > 1 ? members.map((member, index) => member.media?.[0] ? versionLabel(member.media[0], `${index + 1}`) : null).filter(Boolean) as string[]
      : first.versions?.length ? [t("component.workspace_app_view.media_version_count", { count: first.versions.length + 1 })] : [];
    const making = !file && beingMade(first.status);
    const frame = file
      ? <MediaFrame file={file} label={title} aspect={media.aspect || (layout === "masonry" || layout === "board" ? "auto" : undefined)}
        transparent={media.transparent} sizeLabel={media.size_label} hoverPlay={media.hover_play} />
      : making ? <span className="workspace-app-media__frame" style={{ aspectRatio: aspectRatioCss(media.aspect) || "1" }}><MakingCover title={title} percent={first.progress_percent} /></span>
        : <span className="workspace-app-media__frame" data-kind="file" aria-hidden="true"><IconDocument size={22} /></span>;
    // A track, an episode or a voice in a list plays in its row; the row's name opens the viewer.
    if (layout === "list") {
      return <li key={key} className="workspace-app-media__row">
        {kind === "audio" && file
          ? <span className="workspace-app-media__row-player"><MediaViewerRowPlayer file={file} label={title} /></span>
          : <button type="button" className="workspace-app-media__row-thumb" onClick={(event) => open(members, 0, event.currentTarget)} aria-label={title}>{frame}</button>}
        <span className="workspace-app-media__row-copy">
          <button type="button" className="workspace-app-media__row-title" onClick={(event) => open(members, 0, event.currentTarget)}>
            <strong title={title}>{title}</strong>
          </button>
          {first.status && first.status !== "completed" && <small>{t(`component.workflow_run.state.${first.status}`)}{typeof first.progress_percent === "number" ? ` · ${first.progress_percent}%` : ""}</small>}
          {first.fields?.length ? <AppDataFields fields={first.fields} dates={[]} className="workspace-app-data__fields" humanizeText /> : null}
        </span>
        {versions.length > 0 && <span className="workspace-app-media__versions">{versions.map((label) => <small key={label}>{label}</small>)}</span>}
      </li>;
    }
    return <button key={key} type="button" className="workspace-app-media__card" onClick={(event) => open(members, 0, event.currentTarget)}>
      {frame}
      {layout !== "board" && layout !== "strip" && <span className="workspace-app-data__pack-caption">
        <strong title={title}>{title}</strong>
        {first.status && first.status !== "completed" && <small>{t(`component.workflow_run.state.${first.status}`)}{typeof first.progress_percent === "number" ? ` · ${first.progress_percent}%` : ""}</small>}
        {versions.length > 0 && <span className="workspace-app-media__versions">{versions.map((label) => <small key={label}>{label}</small>)}</span>}
      </span>}
    </button>;
  });
  return <div className="workspace-app-media__gallery-block">
    {files.length > 1 && <div className="workspace-app-media__gallery-tools"><DownloadAllButton files={files} /></div>}
    {layout === "carousel"
      ? <MediaCarousel count={cards.length} label={config.title || t("component.workspace_app_view.data_media")}>{cards}</MediaCarousel>
      : layout === "list" ? <ul className="workspace-app-media__list">{cards}</ul>
        : <div className={`workspace-app-media__gallery is-${layout}`}>{cards}</div>}
  </div>;
}

/** A track in a list row: it streams only once pressed, like every track on a page. */
function MediaViewerRowPlayer({ file, label }: { file: NonNullable<WorkspaceAppDataItem["media"]>[number]; label: string }) {
  return <MediaPlayer file={file} label={label} kind="audio" />;
}

function GalleryItems({ items }: { items: WorkspaceAppDataItem[] }) {
  const openRun = useContext(RunOpenerContext);
  const navigate = useNavigate();
  if (!items.length) return null;
  const records = items.some((item) => String(item.media?.[0]?.mime_type || "").toLowerCase().startsWith("audio/"));
  // Results that are only words (a brief, a plan) read as cards of text, not empty covers.
  const text = !items.some((item) => writtenFiles(item).length !== (item.media?.length || 0) || beingMade(item.status));
  // A written result opens to its words, the run one step behind them.
  const openWritten = (item: WorkspaceAppDataItem, title: string) => {
    const files = writtenFiles(item);
    const run = item.run_id && item.workflow_id ? { id: item.run_id, workflow_id: item.workflow_id, binding_id: item.binding_id } : null;
    openDetail({
      title,
      width: 720,
      body: <div className="workspace-app-data__written">{files.map((file) => <section key={file.document_id}>
        {files.length > 1 && <h4>{file.label || file.title}</h4>}
        <InlineDocumentText documentId={file.document_id} kind={inlineDocumentTextKind(file.title, null)!} label={file.label || file.title || title} open />
      </section>)}</div>,
      primaryAction: { label: t("component.workspace_app_view.data_open_document"), onClick: () => { closeDetail(); navigate(`/viewer/${encodeURIComponent(files[0].document_id)}`); } },
      ...(run && openRun ? { secondaryActions: [{ label: t("component.workspace_app_view.data_review_run"), icon: <IconFlow size={16} />, onClick: () => { closeDetail(); openRun(run); } }] } : {}),
    });
  };
  return <div className={`workspace-app-data__packs workspace-app-data__gallery${records ? " workspace-app-data__gallery--records" : text ? " workspace-app-data__gallery--text" : ""}`}>{items.map((item) => {
    const media = item.media?.[0];
    const mime = String(media?.mime_type || "").toLowerCase();
    // A track is a record: a sleeve with the disc behind it, which slides out and turns on hover.
    if (mime.startsWith("audio/")) {
      const run = item.run_id && item.workflow_id ? { id: item.run_id, workflow_id: item.workflow_id, binding_id: item.binding_id } : null;
      const title = item.title || t("component.workspace_app_view.data_untitled");
      const cover = item.media?.find((entry) => String(entry.mime_type || "").toLowerCase().startsWith("image/"));
      const detail = item.fields?.map((field) => field.value).find((value) => typeof value === "string" && value.trim());
      // While its song is being made, a record shows the one making cover every result shows; the
      // sleeve and its disc come once it is made, and the disc slides out only on hover.
      return <button key={item.id} type="button" className="workspace-app-data__record-card" disabled={!run || !openRun}
        onClick={() => run && openRun?.(run)}>
        {inMotion(item.status)
          ? <span className="workspace-app-data__record-stage"><MakingCover title={title} percent={item.progress_percent} /></span>
          : <RecordSleeve cover={cover} title={title} />}
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
    const written = text && writtenFiles(item).length > 0;
    const summary = text && item.fields?.length === 1 && typeof item.fields[0].value === "string" ? item.fields[0].value : null;
    // A picture a project or record names has no run of its own; it opens the picture.
    const viewer = !run && media?.document_id ? `/viewer/${encodeURIComponent(media.document_id)}` : null;
    // A picture with one plain field reads as its caption ("Monday, 9:00 AM"), without that field's label.
    const only = item.fields?.length === 1 ? item.fields[0] : null;
    const caption = !text && only && (!only.format || only.format === "text" || only.format === "date") ? only : null;
    return <button key={item.id} type="button" className="workspace-app-data__pack" disabled={!written && !viewer && (!run || !openRun)}
      onClick={() => written ? openWritten(item, title) : viewer ? navigate(viewer) : run && openRun?.(run)}>
      {!text && <span className="workspace-app-data__pack-cover">
        {pictured ? <PackCover documentId={media!.document_id} label={title} video={mime.startsWith("video/")} thumbnailUrl={media!.thumbnail_url} />
          : !media && beingMade(item.status) ? <MakingCover title={title} percent={item.progress_percent} />
          : <span className="workspace-app-data__gallery-tile" data-kind={media ? mime.split("/")[0] || "file" : "pending"}>
            {mime.startsWith("audio/") ? <IconMusicNote size={22} /> : media ? <IconDocument size={22} /> : null}
          </span>}
        {media && (mime.startsWith("audio/") || mime.startsWith("video/")) && <span className="workspace-app-data__gallery-play" aria-hidden="true"><IconPlay size={12} /></span>}
      </span>}
      <span className="workspace-app-data__pack-caption">
        <strong title={title}>{title}</strong>
        {caption && <small><FieldValue field={caption} dates={[]} humanizeText /></small>}
        {item.status && item.status !== "completed" && <small>{t(`component.workflow_run.state.${item.status}`)}</small>}
      </span>
      {summary ? <span className="workspace-app-data__pack-summary">{summary}</span>
        : item.fields?.length && !caption ? <AppDataFields fields={item.fields} dates={[]} className="workspace-app-data__fields" humanizeText /> : null}
      {text && item.updated_at && <small className="workspace-app-data__pack-date mono">{relativeTime(item.updated_at)}</small>}
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
  const text = !packs.some((pack) => pack.cover);
  return <div className={`workspace-app-data__packs${text ? " workspace-app-data__gallery--text" : ""}`}>{packs.map((pack) => (
    <button key={pack.id} type="button" className="workspace-app-data__pack"
      onClick={(event) => openDetail({
        key: `pack:${pack.id}`, title: pack.label, subtitle: pack.stage, width: 520,
        body: <PackDetail pack={pack} openRun={openRun} action={actionFor(pack)}
          onAction={(action) => { closeDetail(); actions?.open(action.action, action.values); }} />,
        returnFocusTo: event.currentTarget,
      })}>
      {!text && <span className="workspace-app-data__pack-cover"><PackCover documentId={pack.cover?.document_id} label={pack.label} video={pack.coverIsVideo} thumbnailUrl={pack.cover?.thumbnail_url} /></span>}
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

type Narrowing = { filters: Record<string, string>; q: string };

/** A list's own filters and search: each filter offers the values its records hold; search waits for a pause in typing. */
function BlockNarrowing({ block, config, narrowing, onNarrow }: { block: WorkspaceAppDataBlockResult; config?: BlockConfig; narrowing?: Narrowing; onNarrow: (next: Narrowing) => void }) {
  const current = narrowing || { filters: {}, q: "" };
  const [text, setText] = useState(current.q);
  const narrowRef = useRef({ current, onNarrow });
  narrowRef.current = { current, onNarrow };
  useEffect(() => {
    if (text === narrowRef.current.current.q) return;
    const timer = setTimeout(() => narrowRef.current.onNarrow({ ...narrowRef.current.current, q: text }), 300);
    return () => clearTimeout(timer);
  }, [text]);
  return <div className="workspace-app-data__narrowing" role="search" aria-label={t("component.workspace_app_view.data_narrow", { list: block.title || "" })}>
    {(block.filters || []).filter((filter) => filter.options.length > 1 || current.filters[filter.key]).map((filter) =>
      <Select key={filter.key} appearance="ghost" value={current.filters[filter.key] || ""} aria-label={filter.label}
        options={[{ value: "", label: t("component.workspace_app_view.data_filter_all", { label: filter.label }) }, ...filter.options]}
        onChange={(value) => onNarrow({ ...current, filters: Object.fromEntries(Object.entries({ ...current.filters, [filter.key]: value }).filter(([, chosen]) => chosen)) })} />)}
    {config?.search && <span className="workspace-app-data__search">
      <IconSearch size={14} aria-hidden="true" />
      <input type="search" value={text} onChange={(event) => setText(event.target.value)} maxLength={100}
        placeholder={config.search.label || t("component.workspace_app_view.data_search")} aria-label={config.search.label || t("component.workspace_app_view.data_search")} />
    </span>}
  </div>;
}

/** What a post's card offers beside the post: where it stands, its counts once out, and its own buttons. */
function PreviewFooter({ workspaceId, item, stageLabels, itemActions, showStatus }: { workspaceId: string; item: WorkspaceAppDataItem; stageLabels?: Record<string, string>; itemActions: WorkspaceAppCardAction[]; showStatus: boolean }) {
  const stoppedLabel = stoppedLabelOf(item);
  const status = showStatus && item.status ? stoppedLabel || itemStatusLabel(item, stageLabels) : null;
  if (!status && !item.metrics?.length && !itemActions.length && !item.execution) return null;
  return <div className="workspace-app-data__preview-footer">
    {status && <span className={`workspace-app-data__status ${stoppedLabel ? "is-error" : statusClass(item.status)}`}>{status}</span>}
    {item.metrics?.length ? <span className="workspace-app-data__preview-metrics">{item.metrics.map((metric, index) =>
      <span key={metric.id || index} title={metric.collection_error || undefined}><b>{metricValue(metric)}</b> {metricLabel(metric)}</span>)}</span> : null}
    <ItemActionButtons workspaceId={workspaceId} item={item} itemActions={itemActions} variant="outline" />
    {item.execution && item.execution.status !== "completed" && <ExecutionDetails workspaceId={workspaceId} execution={item.execution} detail={item.detail} />}
  </div>;
}

/** Reads one page of a block outside its first one: the next page, a tree's level under a record, an export's pages. */
type BlockReader = (blockKey: string, options: { cursor?: string | null; parent?: string; level?: number; narrowing?: Narrowing }) => Promise<WorkspaceAppDataBlockResult | undefined>;

/** How many records an export reads at most. */
const EXPORT_LIMIT = 5000;
const LIST_VIEWS = new Set(["list", "table", "board"]);

/** A list's own controls: groupings, columns, ticked rows, export, saved views, and the views laid out in time,
 *  place or a tree. Each record opens its whole card in the global detail drawer. */
function useListControls(block: WorkspaceAppDataBlockResult, config: BlockConfig | undefined, listKey: string) {
  const choices = config?.group_by || [];
  const [group, setGroup] = useState(choices.length ? 0 : -1);
  const [sort, setSort] = useState<TableSort | null>(null);
  const columnsKey = `manor:workspace-app-columns:${listKey}`;
  const [hidden, setHidden] = useState<Set<string>>(() => new Set(readStored(columnsKey, [] as string[], isStringList)));
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const anchor = useRef<string | null>(null);
  const publish = useWorkspaceAppSelection((state) => state.setSelected);
  const clear = useWorkspaceAppSelection((state) => state.clear);
  const ids = useMemo(() => block.items.map((item) => item.id).filter(Boolean), [block.items]);
  // A row that left the list (filtered out, gone) is no longer ticked.
  useEffect(() => { setSelected((current) => { const kept = keepListed(current, ids); return kept.size === current.size ? current : kept; }); }, [ids]);
  useEffect(() => { if (config?.select) publish(listKey, [...selected]); }, [config?.select, listKey, publish, selected]);
  useEffect(() => () => clear(listKey), [clear, listKey]);
  const selection: SelectionControl | undefined = config?.select ? {
    selected,
    toggle: (id, order) => {
      const from = anchor.current;
      setSelected((current) => toggleSelection(current, id, { range: Boolean(order), anchor: from, order: order || ids }));
      anchor.current = id;
    },
    setAll: (all, on) => setSelected((current) => {
      const next = new Set(current);
      for (const id of all) { if (on) next.add(id); else next.delete(id); }
      return next;
    }),
  } : undefined;
  const hideColumns = (next: Set<string>) => { setHidden(next); writeStored(columnsKey, [...next]); };
  return { choices, group, setGroup, sort, setSort, hidden, hideColumns, selection, selected, setSelected };
}

function Block({ workspaceId, block, config, title, hideTitle = false, showRollupNext = false, compactDocuments = false, onMore, loadingMore, narrowing, onNarrow, listKey, read }: { workspaceId: string; block: WorkspaceAppDataBlockResult; config?: WorkspaceAppDataComponent["blocks"][number]; title?: string | null; hideTitle?: boolean; showRollupNext?: boolean; compactDocuments?: boolean; onMore: (block: WorkspaceAppDataBlockResult) => void; loadingMore: boolean; narrowing?: Narrowing; onNarrow?: (block: WorkspaceAppDataBlockResult, next: Narrowing) => void; listKey: string; read?: BlockReader }) {
  const blockTitle = title || block.title || block.summary || humanize(block.key);
  // A list people add to by hand offers its Add entry here, opened as its tile opens it (same form, same approvals).
  const actions = useContext(ActionOpenerContext);
  const runOpener = useContext(RunOpenerContext);
  const refreshMedia = useContext(SignedMediaRefreshContext);
  const controls = useListControls(block, config, listKey);
  const [narrowKey, setNarrowKey] = useState(0);
  const view = config?.view || "list";
  const progress = block.type === "progress" && !config?.preview;
  const itemActions = cardActions(config);
  const stageLabels = config?.stage_labels;
  const statusOf = (item: WorkspaceAppDataItem) => stoppedLabelOf(item) || (item.status ? itemStatusLabel(item, stageLabels) : null);
  const openItem = (item: WorkspaceAppDataItem, from: HTMLElement) => openDetail({
    key: `workspace-app-item:${listKey}:${item.id}`, title: itemName(item), subtitle: statusOf(item) || undefined, returnFocusTo: from,
    // The drawer sits outside this view, so it gets the view's openers to keep the card's buttons working.
    body: <SignedMediaRefreshContext.Provider value={refreshMedia}><RunOpenerContext.Provider value={runOpener}><ActionOpenerContext.Provider value={actions}>
      <ol className="workspace-app-data__progress workspace-app-data__detail-card">
        <ProgressItem workspaceId={workspaceId} item={item} stageLabels={stageLabels} itemActions={itemActions} />
      </ol>
    </ActionOpenerContext.Provider></RunOpenerContext.Provider></SignedMediaRefreshContext.Provider>,
  });
  const presenter: RowPresenter = { nameOf: itemName, statusOf, onOpen: openItem };
  const columns = progress && view === "table" ? tableColumns(block.items, config, stageLabels) : [];
  const visibleColumns = columns.filter((column) => column.key === "name" || !controls.hidden.has(column.key));
  const groupChoice = LIST_VIEWS.has(view) && controls.group >= 0 ? controls.choices[controls.group] : undefined;
  const totals = block.totals?.length ? block.totals : undefined;
  const fieldLabel = (path: string[]) => (config?.fields || []).find((field): field is WorkspaceAppSummaryField => !Array.isArray(field) && field.field.join(".") === path.join("."))?.label;
  const exportRows = async (only?: ReadonlySet<string>) => {
    let rows: WorkspaceAppDataItem[] = [];
    if (only) rows = block.items.filter((item) => only.has(item.id));
    else if (read) {
      let cursor: string | null | undefined;
      do {
        const page = await read(block.key, { cursor, narrowing });
        if (!page) break;
        rows.push(...page.items);
        cursor = page.next_cursor;
      } while (cursor && rows.length < EXPORT_LIMIT);
    }
    const all = tableColumns(rows, config, stageLabels).filter((column) => column.key === "name" || !controls.hidden.has(column.key));
    const sorter = controls.sort && all.find((column) => column.key === controls.sort!.column);
    downloadCsv(tableCsv(sorter ? sortTableItems(rows, sorter.value, controls.sort!.direction, getLocale()) : rows, all), csvFileName(blockTitle));
  };
  const selectedTotals = controls.selection && controls.selected.size ? (config?.totals || []).flatMap((total) => {
    const label = fieldLabel(total.field);
    if (!label) return [];
    const fields = block.items.filter((item) => controls.selected.has(item.id)).map((item) => item.fields?.find((field) => field.label === label)).filter(Boolean) as DataField[];
    const value = aggregate(fields.map((field) => field.value), total.agg);
    const text = value === null || !fields[0] ? null : fieldNumberText({ ...fields[0], value }, getLocale());
    return text ? [{ label: `${t(`component.workspace_app_view.totals_${total.agg}`)} · ${label}`, text }] : [];
  }) : [];
  const list = (items: WorkspaceAppDataItem[], withTotals: boolean) => view === "table"
    ? <ProgressTable workspaceId={workspaceId} items={items} columns={visibleColumns} sort={controls.sort} onSort={controls.setSort}
      itemActions={itemActions} title={blockTitle} selection={controls.selection} totals={withTotals ? totals : undefined} onOpen={openItem} />
    : view === "board" ? <ProgressBoard workspaceId={workspaceId} items={items} stages={block.stages} stageLabels={stageLabels} itemActions={itemActions} title={blockTitle} selection={controls.selection} />
      : <ProgressItems workspaceId={workspaceId} items={items} stageLabels={stageLabels} itemActions={itemActions} selection={controls.selection} />;
  const progressBody = view === "timeline" || view === "gantt" || view === "places" || view === "tree" ? (
    view === "timeline" ? <TimelineView items={block.items} presenter={presenter} title={blockTitle} />
      : view === "gantt" ? <GanttView items={block.items} presenter={presenter} title={blockTitle} />
        : view === "places" ? <PlacesView items={block.items} presenter={presenter} title={blockTitle} />
          : <TreeView items={block.items} depth={config?.tree?.children.length || 1} presenter={presenter} title={blockTitle}
            load={async (parent, level, cursor) => {
              const page = read ? await read(block.key, { parent: parent.id, level, cursor }) : undefined;
              return { items: page?.items || [], next_cursor: page?.next_cursor };
            }} />)
    : groupChoice ? <>
      {groupItems(block.items, controls.group).map((group) => <section key={group.key} className="workspace-app-data__list-group" aria-label={group.key || t("component.workspace_app_view.group_empty")}>
        <h5>{group.key || t("component.workspace_app_view.group_empty")}<span className="mono">{group.items.length}</span></h5>
        {list(group.items, false)}
      </section>)}
      {totals && <ListTotals totals={totals} />}
    </> : <>{list(block.items, true)}{totals && view !== "table" && <ListTotals totals={totals} />}</>;
  const body = config?.preview && ["progress", "records", "gallery"].includes(block.type)
    ? <PostPreviewItems items={block.items} config={config.preview} renderFooter={(item) => <PreviewFooter workspaceId={workspaceId} item={item}
      stageLabels={config.stage_labels} itemActions={block.type === "gallery" ? [] : cardActions(config)} showStatus={block.type === "progress"} />} />
    : block.type === "chart" && block.chart ? <WorkspaceAppChart chart={block.chart} title={blockTitle} />
    : block.type === "summary" ? <SummaryItems workspaceId={workspaceId} items={block.items} itemActions={cardActions(config)} /> : block.type === "metrics" ? <MetricItems items={block.items} /> : block.type === "progress" ? progressBody : block.type === "project_rollup" ? <RollupItems workspaceId={workspaceId} items={block.items} showNext={showRollupNext} completion={config?.completion} stageLabels={config?.stage_labels} emptyMessage={config?.empty_message} /> : block.type === "references" ? <ReferenceItems items={block.items} /> : block.type === "gallery" && config?.media ? <MediaGalleryItems items={block.items} config={config} media={config.media} /> : block.type === "gallery" ? <GalleryItems items={block.items} /> : block.type === "packs" ? <PackItems items={block.items} stageLabels={config?.stage_labels} itemAction={config?.item_action} planAction={config?.plan_action} /> : block.type === "documents" && block.items.some((item) => item.folder !== undefined) ? <FolderItems workspaceId={workspaceId} items={block.items} blockTitle={blockTitle} compactDocuments={compactDocuments} /> : <RecordItems workspaceId={workspaceId} items={block.items} documents={block.type === "documents"} blockTitle={blockTitle} compactDocuments={compactDocuments} />;
  const showTitle = !hideTitle && !(block.type === "documents" && block.items.length === 1 && !block.error && blockTitle);
  const compactRollup = block.type === "project_rollup" && !showRollupNext && block.status === "ready"
    && block.items.length === 1 && !block.error && !block.next_cursor;
  const compactDocument = block.type === "documents" && config?.limit === 1 && block.items.length === 1 && !block.error && !block.next_cursor;
  const addAction = config?.add_action && actions?.has(config.add_action.action) ? config.add_action : null;
  const hasItems = block.items.length > 0;
  // Saved views keep what a person can change here: filters, search, grouping and a table's sort.
  const savable = progress && Boolean(read) && Boolean(onNarrow) && Boolean(block.filters?.length || config?.search || controls.choices.length || view === "table");
  const listState: ListState = { filters: narrowing?.filters || {}, q: narrowing?.q || "", group: controls.group, sort: controls.sort };
  const tools = progress && hasItems && (
    (controls.choices.length > 0 && LIST_VIEWS.has(view)) || (view === "table" && columns.length > 2) || (config?.export && read));
  return <section className={`workspace-app-data__block workspace-app-data__block--${block.type}${compactRollup ? " is-compact-rollup" : ""}${config?.completion ? " is-completion-block" : ""}${compactDocument ? " is-compact-document" : ""}`} aria-label={blockTitle}>
    {(showTitle || block.status === "stale" || addAction) && <header>
      {showTitle && <h4>{blockTitle}</h4>}
      {block.status === "stale" && <span className="workspace-app-data__stale">{t("component.workspace_app_view.data_stale")}</span>}
      {addAction && <Button size="sm" variant="outline" className="workspace-app-data__add" onClick={() => actions!.open(addAction.action, {})}>
        <IconPlus size={14} />{addAction.label}
      </Button>}
    </header>}
    {savable && <SavedViewTabs listKey={listKey} state={listState} initial={{ filters: {}, q: "", group: controls.choices.length ? 0 : -1, sort: null }} onApply={(saved) => {
      controls.setGroup(saved.group < controls.choices.length ? saved.group : -1);
      controls.setSort(saved.sort);
      setNarrowKey((key) => key + 1);
      onNarrow!(block, { filters: saved.filters, q: saved.q });
    }} />}
    {/* The list's filters and search, then its own tools at the row's end. */}
    {((onNarrow && (block.filters?.length || config?.search)) || tools) && <div className="workspace-app-data__list-head">
      {onNarrow && (block.filters?.length || config?.search) && <BlockNarrowing key={narrowKey} block={{ ...block, title: blockTitle }} config={config} narrowing={narrowing} onNarrow={(next) => onNarrow(block, next)} />}
      {tools && <div className="workspace-app-data__list-tools">
        {controls.choices.length > 0 && LIST_VIEWS.has(view) && <GroupPicker choices={controls.choices} value={controls.group} onChange={controls.setGroup} />}
        {view === "table" && <ColumnsMenu columns={columns.filter((column) => column.key !== "name")} hidden={controls.hidden} onChange={controls.hideColumns} />}
        {config?.export && read && <ExportButton onExport={() => exportRows()} />}
      </div>}
    </div>}
    {controls.selection && <SelectionBar count={controls.selected.size} totals={selectedTotals} onClear={() => controls.setSelected(new Set())}
      onExport={config?.export ? () => exportRows(controls.selected) : undefined} />}
    {narrowing && !block.items.length && !block.error ? <p className="workspace-app-data__empty">{t("component.workspace_app_view.data_no_match")}</p> : null}
    {block.error || block.status === "error" ? <p className="workspace-app-data__error">{block.error?.message || t("component.workspace_app_view.data_error")}</p> : block.status === "empty" && !block.items.length && config?.empty_message ? <p className="workspace-app-data__empty">{config.empty_message}</p> : body}
    {block.next_cursor && <Button size="sm" variant="ghost" onClick={() => onMore(block)} disabled={loadingMore}>{loadingMore ? t("component.workspace_app_view.data_loading") : t("component.workspace_app_view.data_more")}</Button>}
  </section>;
}

function DefaultWorkspaceAppDataView({ workspaceId, component, preview = false, openRun, openAction }: Props) {
  const visible = usePageVisible();
  const tabId = useId();
  const contextId = useId();
  const tabRefs = useRef<Record<string, HTMLButtonElement | undefined>>({});
  // A view of the whole Workspace has one context, the Workspace itself: its data is read at once,
  // not after asking which contexts there are.
  const workspaceWide = component.context.kind === "workspace" && !component.visibility;
  const draftId = useWorkspaceAppDraftId();
  const contextsKey = ["workspace-app-data-contexts", workspaceId, component.key, component.visibility, draftId];
  const storedContexts = useStoredQueryData<WorkspaceAppDataContextsResponse>(contextsKey, !preview && !workspaceWide);
  const contextsQuery = useInfiniteQuery({
    queryKey: contextsKey,
    initialPageParam: undefined as string | undefined,
    queryFn: ({ signal, pageParam }) => api.workspaces.appViewContexts(workspaceId, component.key, { limit: 50, cursor: pageParam, draftId, signal }),
    getNextPageParam: (lastPage) => lastPage.next_cursor || undefined,
    enabled: visible && !preview && !workspaceWide && Boolean(workspaceId && component.key),
    staleTime: 20_000,
    refetchInterval: (query) => displayReadInterval(query.state, pagedReadInterval(query.state.data?.pages.length)),
    retry: false,
    refetchOnWindowFocus: false,
    ...(storedContexts.value ? { initialData: { pages: [storedContexts.value], pageParams: [undefined] }, initialDataUpdatedAt: storedContexts.savedAt } : {}),
  });
  storedContexts.useSave(contextsQuery.data?.pages[0], contextsQuery.dataUpdatedAt);
  const contexts = useMemo(() => workspaceWide ? [{ kind: "workspace" as const, id: workspaceId, label: component.title || "" }]
    : contextsQuery.data?.pages.flatMap((page) => page.items) ?? EMPTY_CONTEXTS, [workspaceWide, workspaceId, component.title, contextsQuery.data]);
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
  // A list someone narrowed reads its own first page; "more" continues that page.
  const [narrowed, setNarrowed] = useState<Record<string, { narrowing: Narrowing; block?: WorkspaceAppDataBlockResult }>>({});
  const [loadingBlock, setLoadingBlock] = useState<string | null>(null);
  const [moreError, setMoreError] = useState(false);
  const [activeSection, setActiveSection] = useState("");
  const scope = `${workspaceId}:${component.key}:${context?.kind}:${context?.id}:${window}:${component.visibility?.block || ""}`;
  const activeScope = useRef(scope);
  activeScope.current = scope;
  // The newest project leads until the person picks one; a pick stays while it is listed.
  const pickedContext = useRef(false);
  const pickContext = (item: WorkspaceAppDataContext | null) => { pickedContext.current = Boolean(item); setContext(item); };
  useEffect(() => { setContext((current) => (pickedContext.current
    ? contexts.find((item) => item.id === current?.id && item.kind === current?.kind) : undefined) || contexts[0] || null); }, [contexts]);
  useEffect(() => { setExtraBlocks({}); setNarrowed({}); setLoadingBlock(null); setMoreError(false); }, [scope]);
  const dataKey = ["workspace-app-data", workspaceId, component.key, context?.kind, context?.id, window, component.visibility, draftId];
  const dataEnabled = !preview && Boolean(context) && (!cardsMode || opened);
  const storedData = useStoredQueryData<WorkspaceAppDataResponse>(dataKey, dataEnabled);
  const dataQuery = useQuery({
    queryKey: dataKey,
    queryFn: ({ signal }) => api.workspaces.appViewComponentData(workspaceId, component.key, { contextKind: context!.kind, contextId: context!.id, window, draftId, signal }),
    enabled: visible && dataEnabled,
    staleTime: 15_000,
    refetchInterval: (query) => displayReadInterval(query.state, 60_000),
    retry: false,
    refetchOnWindowFocus: false,
    ...storedData.initial,
  });
  storedData.useSave(dataQuery.data, dataQuery.dataUpdatedAt);
  // Expired signed thumbnails ask for this component's data (and covers) once more.
  const { refetch: refetchData } = dataQuery;
  const { refetch: refetchContexts } = contextsQuery;
  // A project that no longer exists (deleted here or elsewhere) is dropped from the view and the list is read again,
  // so the view falls back to a project that is still there.
  useEffect(() => {
    if (!(dataQuery.error instanceof ApiError) || dataQuery.error.status !== 404) return;
    pickedContext.current = false;
    void refetchContexts();
  }, [dataQuery.error, refetchContexts]);
  const refetchSignedMedia = useCallback(() => Promise.all([refetchData(), refetchContexts()]), [refetchData, refetchContexts]);
  const refreshSignedMedia = useSignedMediaRefresh(refetchSignedMedia);
  const blocks = useMemo(() => (dataQuery.data?.blocks || []).map((block) => {
    const own = narrowed[block.key];
    if (own) return own.block ? { ...own.block, filters: own.block.filters || block.filters } : { ...block, items: [], next_cursor: null };
    const extra = extraBlocks[block.key];
    return extra ? {
      ...block,
      items: Array.from(new Map([...block.items, ...extra.items].map((item) => [item.id, item])).values()),
      next_cursor: extra.next_cursor,
      error: extra.error || block.error,
    } : block;
  }), [dataQuery.data?.blocks, extraBlocks, narrowed]);
  const narrow = async (block: WorkspaceAppDataBlockResult, narrowing: Narrowing) => {
    if (!context) return;
    if (!narrowing.q.trim() && !Object.keys(narrowing.filters).length) {
      setNarrowed(({ [block.key]: _cleared, ...rest }) => rest);
      return;
    }
    setNarrowed((current) => ({ ...current, [block.key]: { narrowing } }));
    const requestScope = scope;
    try {
      const page = await api.workspaces.appViewComponentData(workspaceId, component.key, {
        contextKind: context.kind, contextId: context.id, block: block.key, limit: 100, window, draftId,
        filters: narrowing.filters, q: narrowing.q.trim() || undefined,
      });
      const next = page.blocks.find((item) => item.key === block.key);
      if (activeScope.current === requestScope && next) setNarrowed((current) => current[block.key]?.narrowing === narrowing
        ? { ...current, [block.key]: { narrowing, block: next } } : current);
    } catch {
      if (activeScope.current === requestScope) setMoreError(true);
    }
  };
  const loadMore = async (block: WorkspaceAppDataBlockResult) => {
    if (!context || !block.next_cursor) return;
    const own = narrowed[block.key];
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
        draftId,
        ...(own ? { filters: own.narrowing.filters, q: own.narrowing.q.trim() || undefined } : {}),
      });
      if (activeScope.current !== requestScope) return;
      const next = page.blocks.find((item) => item.key === block.key);
      if (next && own) setNarrowed((current) => current[block.key]?.block ? { ...current, [block.key]: {
        ...current[block.key], block: { ...next, items: [...current[block.key].block!.items, ...next.items] } } } : current);
      else if (next) setExtraBlocks((current) => ({
        ...current,
        [block.key]: { ...next, items: [...(current[block.key]?.items || []), ...next.items] },
      }));
    } catch {
      if (activeScope.current === requestScope) setMoreError(true);
    } finally {
      if (activeScope.current === requestScope) setLoadingBlock(null);
    }
  };
  const previewBlocks: WorkspaceAppDataBlockResult[] = component.blocks.map((block) => ({ ...block, filters: undefined, chart: undefined, totals: undefined, status: "waiting", items: [], summary: block.title || null, next_cursor: null }));
  const shownBlocks = preview ? previewBlocks : blocks;
  const configuredBlocks = new Map(component.blocks.map((block) => [block.key, block]));
  const addAction = (key: string) => configuredBlocks.get(key)?.add_action?.action;
  const renderedBlocks = shownBlocks.filter((block) => preview || Boolean(block.items.length || block.error || block.next_cursor || narrowed[block.key])
    || (block.status === "empty" && Boolean(configuredBlocks.get(block.key)?.empty_message))
    // An empty list people add to by hand still shows, so its Add button can add the first one.
    || ((block.status === "empty" || block.status === "ready") && Boolean(addAction(block.key) && openAction?.has(addAction(block.key)!)))
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
  const readBlock: BlockReader | undefined = preview || !context ? undefined : async (blockKey, options) => {
    const page = await api.workspaces.appViewComponentData(workspaceId, component.key, {
      contextKind: context.kind, contextId: context.id, block: blockKey, cursor: options.cursor || undefined, limit: 100, window, draftId,
      parent: options.parent, level: options.level,
      ...(options.narrowing ? { filters: options.narrowing.filters, q: options.narrowing.q.trim() || undefined } : {}),
    });
    return page.blocks.find((item) => item.key === blockKey);
  };
  const content = <div
    id={`${tabId}-${displaySection}-panel`}
    className={`workspace-app-data__content${component.display?.layout === "grid" ? " is-grid" : ""}`}
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
          narrowing={narrowed[block.key]?.narrowing}
          onNarrow={preview ? undefined : narrow}
          listKey={`${workspaceId}:${component.key}:${block.key}`}
          read={readBlock}
        />;
      })}
    </div>)}
  </div>;
  // What Flows made shows once something has been made; an empty shelf is not shown.
  const galleryOnly = component.blocks.every((block) => block.type === "gallery");
  if (galleryOnly && !preview && dataQuery.isSuccess && !activeSections.length) return null;
  return <SignedMediaRefreshContext.Provider value={refreshSignedMedia}><RunOpenerContext.Provider value={openRun || null}><ActionOpenerContext.Provider value={openAction || null}><section className="workspace-app-data" aria-label={component.title || component.key}>
    {/* A view without a title sits under the page's own heading; its key is not a name to show. */}
    {(component.title || component.description) && <header className="workspace-app-data__heading">
      {component.title && <h3>{component.title}</h3>}
      {component.description && <OneLineText as="p" text={component.description} />}
    </header>}
    {(showContextRow || (!preview && hasMetricResults)) && <div className="workspace-app-data__filters">
    {showContextRow && <div className="workspace-app-data__context-row">
      {!sameDisplayLabel(component.title, contextLabel) && <label htmlFor={contexts.length > 1 ? contextId : undefined}>{contextLabel}</label>}
      <div className="workspace-app-data__context-controls">
        {contexts.length > 1 ? <select
          id={contextId}
          aria-label={contextLabel}
          title={context ? context.created_at ? `${context.label} · ${formatDateLong(context.created_at)}` : context.label : undefined}
          value={context?.id || ""}
          onChange={(event) => pickContext(contexts.find((item) => item.id === event.target.value) || null)}
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
          : contexts.length ? <ContextCards contexts={contexts} onOpen={(item) => { pickContext(item); setOpened(true); }} />
            : <span className="workspace-app-data__status workspace-app-data__awaiting">{t("component.workspace_app_view.data_awaiting")}</span>
      : preview ? content
      : contextsQuery.isLoading || (context && dataQuery.isLoading) ? <p className="workspace-app-data__loading"><IconRefresh size={15} aria-hidden="true" /> {t("component.workspace_app_view.data_loading")}</p>
        : contextsQuery.isError || dataQuery.isError ? <p className="workspace-app-data__error">{t("component.workspace_app_view.data_error")}</p>
          : activeSections.length ? content
            : <span className="workspace-app-data__status workspace-app-data__awaiting">{t("component.workspace_app_view.data_awaiting")}</span>}
    {moreError && <p className="workspace-app-data__error">{t("component.workspace_app_view.data_error")}</p>}
  </section></ActionOpenerContext.Provider></RunOpenerContext.Provider></SignedMediaRefreshContext.Provider>;
}

function CalendarItemDetails({ workspaceId, item, records, itemActions }: { workspaceId: string; item: WorkspaceAppDataItem; records: WorkspaceAppDataItem[]; itemActions: WorkspaceAppCardAction[] }) {
  const fieldKey = (field: NonNullable<WorkspaceAppDataItem["fields"]>[number]) => JSON.stringify([field.label, field.value, field.format, field.href, field.links, field.provider, field.secondary, field.records, field.members, field.tags, field.images, field.end]);
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
    {/* Beside a post's own answer (Review), its action is the quieter choice. */}
    <ItemActionButtons workspaceId={workspaceId} item={item} itemActions={itemActions} variant="outline" />
    <PublishLateNow workspaceId={workspaceId} item={item} />
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

export default function WorkspaceAppDataView(props: Props) {
  const { workspaceId, component, preview = false, onFocusChange } = props;
  if (!component.display?.calendar) return <DefaultWorkspaceAppDataView {...props} />;
  const calendarBlock = component.blocks.find((block) => block.key === component.display?.calendar?.block);
  const itemActions = cardActions(calendarBlock);
  // A post's result action sits on the progress block its entries come from, not on the calendar's own block.
  const resultAction = calendarBlock?.result_action ?? component.blocks.find((block) => block.result_action)?.result_action;
  return <RunOpenerContext.Provider value={props.openRun || null}><ActionOpenerContext.Provider value={props.openAction || null}><WorkspaceAppCalendarView
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
      listKey={`${workspaceId}:${component.key}:${block.key}`}
    />}
    renderItemDetails={(item, records) => <CalendarItemDetails workspaceId={workspaceId} item={item} records={records} itemActions={itemActions} />}
    renderItemMetrics={(item, records) => <div className="workspace-app-data__results-row">
      <div className="workspace-app-data__results-values">{records.length ? records.map((record) => <div key={record.id}>
        {record.conflict && <p className="workspace-app-data__error">{t("component.workspace_app_view.data_publication_conflict", { detail: record.conflict })}</p>}
        {record.metrics?.length ? <MetricItems items={record.metrics} linked={false} presentation="compact" /> : <span className="workspace-app-data__tracking-notice">{t(`component.workspace_app_view.data_tracking_${record.tracking_status || "pending"}`)}</span>}
      </div>) : <span className="workspace-app-data__tracking-notice">{t("component.workspace_app_view.data_empty")}</span>}</div>
      <ItemResultsAction workspaceId={workspaceId} item={item} itemAction={resultAction} />
    </div>}
    renderItemResponse={(item) => <CalendarItemResponse workspaceId={workspaceId} item={item} />}
    onFocusChange={onFocusChange}
  /></ActionOpenerContext.Provider></RunOpenerContext.Provider>;
}
