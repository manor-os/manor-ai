import { Component, lazy, Suspense, useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  type WorkspaceAppMetric,
  type WorkspaceAppOutputFile,
  type WorkspaceAppOutputRun,
  type WorkspaceAppQueueItem,
  type WorkspaceAppSection,
  type WorkspaceAppSectionBlock,
  type WorkspaceAppSectionData,
  type WorkspaceConnectionRequirement,
  type WorkspaceAssessmentTemplate,
  type WorkspaceStatDefinition,
} from "../../lib/api";
import { captureClientError } from "../../lib/clientErrors";
import { displayReadInterval } from "../../lib/readRefreshPolicy";
import { usePageVisible } from "../../lib/usePageVisible";
import { viewerPathForDocumentId } from "../../lib/fileReferences";
import { getPreferredTimeZone, relativeTime } from "../../lib/format";
import { goalProgressPercent } from "../../lib/goalNumbers";
import { formatUserFacingText } from "../../lib/taskDisplay";
import { getLocale, t } from "../../lib/i18n";
import { workflowRunPath } from "../../lib/workflowRoutes";
import { usePreviewFeatureAccess } from "../../lib/previewFeatureAccess";
import { formatWorkspaceStatValue, workspaceStatAttentionPresentation } from "../../lib/workspaceStats";
import { IconArrowRight, IconBell, IconChevronRight, IconMusicNote, IconPause, IconPlay, IconWarning } from "../icons";
import { useToastStore } from "../../stores/toast";
import Button from "../ui/Button";
import { useDocumentThumbnail } from "../ui/useDocumentThumbnail";
import IntegrationConnectRow from "../integrations/IntegrationConnectRow";
import { useWorkspaceAppDraftId } from "./workspaceAppDraft";

const WorkspaceAppDecisionDialog = lazy(() => import("./WorkspaceAppDecisionDialog"));
import { openMetricDetail } from "./WorkspaceAppMetricDetail";
import { fileVisual, metricPhaseTone, runStatusLabel } from "./workspaceAppRunDisplay";

const ACTIVE_RUN_STATUSES = new Set(["pending", "running"]);
const TERMINAL_RUN_STATUSES = new Set(["completed", "failed", "cancelled"]);
/** Shown at once when an older server sends no limit for the metrics block. */
const DEFAULT_HEADLINE_METRICS = 4;
/** Phases that need a person even before a first reading exists. */
const ATTENTION_PHASES = new Set(["configuration_error", "assessment_error", "collection_error", "insufficient_evidence", "stale"]);


/** Fill list fields an older or newer server may omit, so blocks never read undefined. */
function normalizeSectionData(data: WorkspaceAppSectionData): WorkspaceAppSectionData {
  const items = Array.isArray(data.items) ? data.items : [];
  if (data.block === "outputs") {
    return { ...data, items: (items as WorkspaceAppOutputRun[]).map((run) => {
      const files = Array.isArray(run.files) ? run.files : [];
      const progress = run.progress && Array.isArray(run.progress.stages) ? run.progress : null;
      return {
        ...run, files, file_count: run.file_count ?? files.length, progress,
        publications: Array.isArray(run.publications) ? run.publications : [],
      };
    }) };
  }
  if (data.block === "connections") return { ...data, items: items as WorkspaceConnectionRequirement[] };
  if (data.block === "metrics") {
    return { ...data, items: (items as WorkspaceAppMetric[]).map((metric) => ({
      ...metric,
      series: Array.isArray(metric.series) ? metric.series : [],
      gaps: Array.isArray(metric.gaps) ? metric.gaps : [],
      goal: metric.goal ?? null,
      collector_type: metric.collector_type ?? null,
      next_assessment_at: metric.next_assessment_at ?? null,
    })), suggestions: Array.isArray(data.suggestions) ? data.suggestions : [] };
  }
  return { ...data, items } as WorkspaceAppSectionData;
}

/** One block's data query; blocks and the notices zone share its cache. */
function sectionQueryOptions(workspaceId: string, section: WorkspaceAppSection, refetchInterval: (data: unknown) => number | false,
  draftId: string | null, visible: boolean) {
  return {
    queryKey: ["workspace-app-section", workspaceId, section.id, section.source, section.view, section.limit, draftId],
    queryFn: async ({ signal }: { signal: AbortSignal }) => normalizeSectionData(await api.workspaces.appViewSection(workspaceId, section.id, signal, draftId)),
    enabled: visible && section.available,
    refetchInterval: (query: { state: { data?: unknown; status?: string; error?: unknown } }) => displayReadInterval(query.state, refetchInterval(query.state.data)),
    retry: false,
    refetchOnWindowFocus: "always" as const,
  };
}

function useSectionData(workspaceId: string, section: WorkspaceAppSection, refetchInterval: (data: unknown) => number | false) {
  return useQuery(sectionQueryOptions(workspaceId, section, refetchInterval, useWorkspaceAppDraftId(), usePageVisible()));
}

function fileHref(file: WorkspaceAppOutputFile): string | null {
  return viewerPathForDocumentId(file.document_id) || file.open_url || null;
}

function runStatusTone(run: WorkspaceAppOutputRun): "active" | "done" | "attention" | "failed" | "idle" {
  if (ACTIVE_RUN_STATUSES.has(run.status)) return "active";
  if (run.status === "failed") return "failed";
  if (run.status === "paused" || (run.status === "completed" && run.business_outcome && run.business_outcome !== "completed")) return "attention";
  if (run.status === "completed") return "done";
  return "idle";
}

/** A finished result speaks for itself; only a state that needs noticing (running, waiting, failed) gets a chip. */
function runSettled(run: WorkspaceAppOutputRun): boolean {
  return run.status === "completed" && !run.business_outcome?.match(/needs_input|revision_required/);
}

function SectionHead({ title, detail, action }: { title: string; detail?: string | null; action?: ReactNode }) {
  return <div className="workspace-app-view__section-head">
    <h3>{title}</h3>
    {detail && <span className="workspace-app-section__detail">{detail}</span>}
    {action}
  </div>;
}

function SectionError({ onRetry }: { onRetry: () => void }) {
  return <div className="workspace-app-section__note" role="alert">
    <span>{t("component.workspace_app_view.section_error")}</span>
    <Button variant="ghost" size="sm" onClick={onRetry}>{t("component.workspace_app_view.retry")}</Button>
  </div>;
}

function OutputsSection({ workspaceId, section, runAction }: {
  workspaceId: string;
  section: WorkspaceAppSection;
  runAction?: (run: WorkspaceAppOutputRun) => string | null;
}) {
  const location = useLocation();
  const navigate = useNavigate();
  const flowsAccess = usePreviewFeatureAccess("flows");
  // Follow unsettled runs closely, but keep discovering runs started elsewhere
  // even when this list is empty or every known result has finished.
  const query = useSectionData(workspaceId, section, (data) => {
    const items = (data as { items?: WorkspaceAppOutputRun[] } | undefined)?.items || [];
    return items.some((run) => !TERMINAL_RUN_STATUSES.has(run.status)) ? 10_000 : 60_000;
  });
  const runs = (query.data?.block === "outputs" ? query.data.items : []) as WorkspaceAppOutputRun[];
  // A section is named by its config, else after the flow it is scoped to, so two output sections differ.
  const title = section.title || section.source_title || t("component.workspace_app_view.outputs_title");
  const head = <SectionHead title={title} detail={!section.title && section.source_title ? t("component.workspace_app_view.outputs_title") : null} />;

  if (!section.available) {
    return <section className="workspace-app-view__section workspace-app-section" aria-label={title}>
      {head}
      <p className="workspace-app-section__note">{t("component.workspace_app_view.section_unavailable")}</p>
    </section>;
  }
  if (query.isPending) {
    return <section className="workspace-app-view__section workspace-app-section" aria-label={title} aria-busy="true">
      {head}
      <div className="workspace-app-section__skeleton" aria-hidden="true"><span /><span /><span /></div>
    </section>;
  }
  if (query.isError) {
    return <section className="workspace-app-view__section workspace-app-section" aria-label={title}>
      {head}
      <SectionError onRetry={() => void query.refetch()} />
    </section>;
  }
  // The default page only shows outputs once the Workspace has produced some.
  if (!runs.length && section.implicit) return null;
  const listed = runs.filter(listedOutput);

  const runState = { returnTo: `${location.pathname}${location.search}${location.hash}` };
  // Results come first: the gallery, unless someone chose the list.
  const gallery = section.view !== "list";
  // A result opens where it was made: a request at its reply, a run in its App entry.
  const openRun = (run: WorkspaceAppOutputRun): (() => void) | null => {
    if (run.kind === "chat") {
      return run.message_id ? () => navigate({
        pathname: location.pathname, search: location.search, hash: `workspace-chat-message-${run.message_id}`,
      }) : null;
    }
    const actionKey = runAction?.(run);
    if (actionKey) {
      return () => navigate(`${location.pathname}${location.search}`, {
        state: { ...location.state, workspaceApp: { workspaceId, actionKey, runId: run.run_id } },
      });
    }
    return flowsAccess.enabled ? () => navigate(workflowRunPath(run.workflow_id, run.run_id, workspaceId), { state: runState }) : null;
  };
  return <section className="workspace-app-view__section workspace-app-section" aria-label={title}>
    {head}
    {!runs.length || (!gallery && !listed.length) ? <p className="workspace-app-section__note">{t("component.workspace_app_view.outputs_empty")}</p>
      : gallery ? <OutputsGallery runs={runs} openRun={openRun} />
        : <ul className="workspace-app-outputs">{listed.map((run) => {
          const tone = runStatusTone(run);
          // Name what came out; the action that made it is the byline.
          const action = run.title || section.source_title || t("component.workspace_app_view.outputs_run");
          const name = run.result_title || action;
          const when = relativeTime(run.started_at || run.created_at, "");
          const label = <><strong className="workspace-app-outputs__name">{name}</strong>
            <small>{name === action ? when : `${action} · ${when}`}</small></>;
          // The result opens in the App entry that made it; the full run page
          // belongs to Flows and is offered only where Flows is open.
          const actionKey = run.kind === "chat" ? null : runAction?.(run);
          return <li key={run.run_id} className="workspace-app-outputs__run">
            <div className="workspace-app-outputs__head">
              {/* A request sent to an Agent opens at its reply in chat, as a pending decision opens its card. */}
              {run.kind === "chat" && run.message_id ? <button type="button" className="workspace-app-outputs__open" onClick={() => navigate({
                pathname: location.pathname, search: location.search, hash: `workspace-chat-message-${run.message_id}`,
              })}>{label}</button>
              : actionKey ? <button type="button" className="workspace-app-outputs__open" onClick={() => navigate(
                `${location.pathname}${location.search}`,
                { state: { ...location.state, workspaceApp: { workspaceId, actionKey, runId: run.run_id } } },
              )}>{label}</button>
                : flowsAccess.enabled ? <Link to={workflowRunPath(run.workflow_id, run.run_id, workspaceId)} state={runState}>{label}</Link>
                  : <span className="workspace-app-outputs__open">{label}</span>}
              {!runSettled(run) && <span className={`workspace-app-status workspace-app-status--${tone}`}>{runStatusLabel(run.status, run.business_outcome)}</span>}
            </div>
            <RunStageProgress run={run} />
            {run.result_summary && run.result_summary !== name && <p className="workspace-app-outputs__summary">{run.result_summary}</p>}
            {run.files.length > 0 && <ul className="workspace-app-outputs__files">
              {run.files.map((file) => <OutputFileLink key={file.document_id || file.fs_path || file.name} file={file} />)}
              {run.file_count > run.files.length && <li className="workspace-app-outputs__more">
                {t("component.workspace_app_view.outputs_more_files", { count: run.file_count - run.files.length })}
              </li>}
            </ul>}
            {(run.publications || []).length > 0 && <ul className="workspace-app-outputs__published" aria-label={t("component.workspace_app_view.outputs_published_list")}>
              {(run.publications || []).map((publication, index) => {
                const label = [publication.platform, publication.title].filter(Boolean).join(" · ");
                const verified = publication.verification_status === "verified";
                return <li key={`${publication.platform}:${publication.published_url || index}`}>
                  {publication.published_url
                    ? <a href={publication.published_url} target="_blank" rel="noopener noreferrer">{label} ↗</a>
                    : <span>{label}</span>}
                  <small>{t(verified ? "component.workspace_app_view.outputs_published" : "component.workspace_app_view.outputs_unverified")}</small>
                </li>;
              })}
            </ul>}
          </li>;
        })}</ul>}
  </section>;
}

/** Where an unfinished run is, from the stages its persisted summary exposes. */
function RunStageProgress({ run }: { run: WorkspaceAppOutputRun }) {
  const progress = run.progress;
  const current = progress?.current;
  if (!progress || !current || !progress.stage_count) return null;
  const label = current.waiting
    ? t("component.workspace_app_view.outputs_waiting", { name: current.name || "" })
    : current.status === "failed"
      ? t("component.workspace_app_view.outputs_stopped", { name: current.name || "" })
      : t("component.workspace_app_view.outputs_step", {
        current: (current.index ?? progress.done_count) + 1, total: progress.stage_count, name: current.name || "",
      });
  return <div className="workspace-app-outputs__progress">
    <span className="workspace-app-outputs__stages" aria-hidden="true">
      {progress.stages.map((stage, index) => <i key={index} className={
        index === current.index ? (current.waiting ? "is-waiting" : current.status === "failed" ? "is-failed" : "is-current")
          : ["completed", "skipped"].includes(stage.status) ? "is-done" : ""
      } />)}
    </span>
    <small>{label}</small>
  </div>;
}

function OutputFileLink({ file }: { file: WorkspaceAppOutputFile }) {
  const href = fileHref(file);
  const { extension, Icon } = fileVisual(file.name);
  const content = <>
    <Icon size={15} aria-hidden="true" />
    <span>{file.name || t("component.workspace_app_view.file")}</span>
    {extension && <small>{extension}</small>}
  </>;
  return <li>{href ? <Link to={href} title={file.name}>{content}</Link> : <span>{content}</span>}</li>;
}

/** The document's own preview image; the tile keeps its icon until one loads. */
function OutputThumbnail({ file, onShape }: { file: WorkspaceAppOutputFile; onShape?: (shape: "wide" | "tall") => void }) {
  const thumbnail = useDocumentThumbnail(file.document_id, file.name);
  const kind = thumbnail.kind;
  const [broken, setBroken] = useState(false);
  if (!thumbnail.src || broken) return null;
  return <>
    {/* A page or slide is shown whole; a picture or frame fills the tile. */}
    <img className={`workspace-app-gallery__thumb${kind === "page" || kind === "presentation" ? " workspace-app-gallery__thumb--page" : ""}`}
      src={thumbnail.src} alt="" loading="lazy" onError={() => setBroken(true)}
      onLoad={(event) => {
        const { naturalWidth, naturalHeight } = event.currentTarget;
        if (naturalWidth > naturalHeight * 1.15) onShape?.("wide");
        // A page is portrait by nature; only a picture or frame that tall is a vertical one.
        else if ((kind === "video" || kind === "image") && naturalHeight > naturalWidth * 1.5) onShape?.("tall");
      }} />
    {kind === "video" && <span className="workspace-app-gallery__badge" aria-hidden="true"><IconPlay size={12} /></span>}
  </>;
}

/**
 * Whether a run without files still has a result to show: work in progress, a
 * result waiting on the person, or one that says or published something. A
 * failed or empty run is not an output (Needs you offers its retry).
 */
function resultWithoutFiles(run: WorkspaceAppOutputRun): boolean {
  if (ACTIVE_RUN_STATUSES.has(run.status) || run.status === "paused") return true;
  if (run.business_outcome === "needs_input" || run.business_outcome === "revision_required") return true;
  return run.status === "completed"
    && Boolean(run.result_title || run.result_summary || (run.publications || []).length);
}

/**
 * Whether the list shows a run: one in progress, waiting on the person, or
 * finished with something to show. A failed or cancelled run is not a result
 * (Needs you offers its retry), whatever its steps touched along the way.
 */
function listedOutput(run: WorkspaceAppOutputRun): boolean {
  if (run.status === "failed" || run.status === "cancelled") return false;
  return run.files.length > 0 || resultWithoutFiles(run);
}

/** Where a published result lives, on the tile of what was published. */
function PublishedMarks({ run }: { run: WorkspaceAppOutputRun }) {
  const publications = run.publications || [];
  if (!publications.length) return null;
  return <span className="workspace-app-gallery__published">{publications.map((publication, index) => {
    const verified = publication.verification_status === "verified";
    const label = `${publication.platform || ""}${verified ? " ✓" : ""}`;
    return publication.published_url
      ? <a key={`${publication.platform}:${index}`} href={publication.published_url} target="_blank" rel="noopener noreferrer"
        title={t(verified ? "component.workspace_app_view.outputs_published" : "component.workspace_app_view.outputs_unverified")}
        onClick={(event) => event.stopPropagation()}>{label} ↗</a>
      : <span key={`${publication.platform}:${index}`}>{label}</span>;
  })}</span>;
}

/**
 * Results first: every file as its own preview (a page, a slide, a frame), and a
 * result without files as a card with what it says and its state. What was
 * asked for and which Flow made it stay out of the way.
 */
function OutputsGallery({ runs, openRun }: {
  runs: WorkspaceAppOutputRun[];
  openRun: (run: WorkspaceAppOutputRun) => (() => void) | null;
}) {
  const tiles = runs.flatMap((run): Array<{ key: string; run: WorkspaceAppOutputRun; file: WorkspaceAppOutputFile | null; first: boolean }> => run.files.length
    ? run.files.map((file, index) => ({ key: `${run.run_id}:${file.document_id || file.name}`, run, file, first: index === 0 }))
    : resultWithoutFiles(run) ? [{ key: run.run_id, run, file: null, first: true }] : []);
  if (!tiles.length) return <p className="workspace-app-section__note">{t("component.workspace_app_view.outputs_empty")}</p>;
  return <ul className="workspace-app-gallery">{tiles.map(({ key, run, file, first }) => {
    if (!file) {
      const open = openRun(run);
      const running = ACTIVE_RUN_STATUSES.has(run.status);
      const headline = run.result_title || run.result_summary || (running ? run.progress?.current?.name : "") || run.title;
      const detail = run.result_title && run.result_summary !== run.result_title ? run.result_summary : null;
      const settled = runSettled(run);
      const body = <>
        <span className="workspace-app-gallery__result">
          {!settled && <span className={`workspace-app-status workspace-app-status--${runStatusTone(run)}`}>{runStatusLabel(run.status, run.business_outcome)}</span>}
          <strong>{headline}</strong>
          {detail && <span className="workspace-app-gallery__detail">{detail}</span>}
          <PublishedMarks run={run} />
        </span>
        {running && <GalleryRunProgress run={run} />}
        <span className="workspace-app-gallery__caption"><span>{relativeTime(run.started_at || run.created_at, "")}</span></span>
      </>;
      const className = `workspace-app-gallery__card workspace-app-gallery__card--result${running ? " is-running" : ""}`;
      return <li key={key}>{open
        ? <button type="button" className={className} onClick={open}>{body}</button>
        : <span className={className}>{body}</span>}</li>;
    }
    return <GalleryFileTile key={key} run={run} file={file} first={first} />;
  })}</ul>;
}

/** A running result's stages as segments, the current one filling; one sliding track when stages are unknown. */
function GalleryRunProgress({ run }: { run: WorkspaceAppOutputRun }) {
  const progress = run.progress;
  const current = progress?.current;
  if (!progress?.stage_count || !current) {
    return <span className="workspace-app-gallery__progress" aria-hidden="true">
      <span className="workspace-app-gallery__segments"><i className="is-current" /></span>
    </span>;
  }
  const position = (current.index ?? progress.done_count) + 1;
  return <span className="workspace-app-gallery__progress"
    aria-label={t("component.workspace_app_view.outputs_step", { current: position, total: progress.stage_count, name: current.name || "" })}>
    <span className="workspace-app-gallery__segments" aria-hidden="true">
      {progress.stages.map((stage, index) => <i key={index} className={
        index === current.index ? (current.waiting ? "is-waiting" : "is-current")
          : ["completed", "skipped"].includes(stage.status) ? "is-done" : ""
      } />)}
    </span>
    <small aria-hidden="true">{position}/{progress.stage_count}</small>
  </span>;
}

const AUDIO_FILE = /\.(mp3|wav|flac|m4a|ogg|aac|aiff)$/i;

function formatPlayTime(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds || 0));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

/** A recording plays where it lies: one press loads and plays it, the bar shows how far it is. */
function GalleryAudioTile({ run, file, first }: { run: WorkspaceAppOutputRun; file: WorkspaceAppOutputFile; first: boolean }) {
  const audio = useRef<HTMLAudioElement | null>(null);
  const [state, setState] = useState<"idle" | "loading" | "playing" | "paused">("idle");
  const [time, setTime] = useState({ at: 0, length: 0 });
  const href = fileHref(file);
  const { extension } = fileVisual(file.name);
  useEffect(() => () => {
    const element = audio.current;
    element?.pause();
    if (element?.src.startsWith("blob:")) URL.revokeObjectURL(element.src);
  }, []);
  const toggle = async () => {
    if (state === "playing") { audio.current?.pause(); return; }
    if (audio.current) { void audio.current.play(); return; }
    if (!file.document_id) return;
    setState("loading");
    try {
      // It plays while it downloads.
      const element = new Audio(await api.documents.streamUrl(file.document_id));
      element.onplay = () => setState("playing");
      element.onpause = () => setState("paused");
      element.onended = () => setState("paused");
      element.ontimeupdate = () => setTime({ at: element.currentTime, length: element.duration || 0 });
      element.onloadedmetadata = () => setTime({ at: 0, length: element.duration || 0 });
      audio.current = element;
      await element.play();
    } catch {
      audio.current = null;
      setState("idle");
    }
  };
  const progress = time.length ? Math.min(100, time.at / time.length * 100) : 0;
  return <li>
    <div className="workspace-app-gallery__card workspace-app-gallery__card--audio">
      <span className="workspace-app-gallery__glyph" aria-hidden="true"><IconMusicNote size={16} /></span>
      {first && <PublishedMarks run={run} />}
      {file.document_id && <button type="button" className="workspace-app-gallery__play" onClick={() => void toggle()}
        aria-label={t(state === "playing" ? "component.workspace_app_view.outputs_pause" : "component.workspace_app_view.outputs_play", { name: file.name })}
        aria-busy={state === "loading"}>
        {state === "playing" ? <IconPause size={16} /> : <IconPlay size={16} />}
      </button>}
      {/* The clock sits under the button so the caption keeps the file's name. */}
      {time.length > 0 && <span className="workspace-app-gallery__time" aria-hidden="true">
        {formatPlayTime(time.at)} / {formatPlayTime(time.length)}
      </span>}
      <span className="workspace-app-gallery__caption">
        {href ? <Link to={href} title={file.name}>{file.name}</Link> : <span>{file.name}</span>}
        {extension && <small>{extension}</small>}
      </span>
      <span className="workspace-app-gallery__played" aria-hidden="true"><i style={{ width: `${progress}%` }} /></span>
    </div>
  </li>;
}

/**
 * One file as its preview, shaped by what it is: a recording plays in place, a
 * landscape page or frame takes two columns so it reads at size, and a portrait
 * frame (a short, a vertical episode) stands at 9:16.
 */
function GalleryFileTile({ run, file, first }: { run: WorkspaceAppOutputRun; file: WorkspaceAppOutputFile; first: boolean }) {
  const [shape, setShape] = useState<"wide" | "tall" | null>(null);
  if (AUDIO_FILE.test(file.name)) return <GalleryAudioTile run={run} file={file} first={first} />;
  const href = fileHref(file);
  const { extension, Icon } = fileVisual(file.name);
  const body = <>
    <span className="workspace-app-gallery__glyph" aria-hidden="true"><Icon size={22} /></span>
    <OutputThumbnail file={file} onShape={setShape} />
    {first && <PublishedMarks run={run} />}
    <span className="workspace-app-gallery__caption"><span>{file.name}</span>{extension && <small>{extension}</small>}</span>
  </>;
  return <li className={shape ? `workspace-app-gallery__${shape}` : undefined}>
    {href ? <Link className="workspace-app-gallery__card" to={href} title={file.name}>{body}</Link> : <span className="workspace-app-gallery__card">{body}</span>}
  </li>;
}

function Sparkline({ series }: { series: WorkspaceAppMetric["series"] }) {
  if (series.length < 2) return null;
  const values = series.map((point) => point.value);
  const min = Math.min(...values);
  const span = Math.max(...values) - min || 1;
  const points = values.map((value, index) => [index / (values.length - 1) * 100, 26 - (value - min) / span * 22]);
  const line = points.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const [lastX, lastY] = points[points.length - 1];
  return <svg className="workspace-app-metric__spark" viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden="true">
    <polygon points={`0,30 ${line} 100,30`} />
    <polyline points={line} vectorEffect="non-scaling-stroke" />
    <circle cx={lastX} cy={lastY} r="2.2" />
  </svg>;
}

/** Change since the previous observation: a percentage, or an absolute step from zero. */
function metricChange(series: WorkspaceAppMetric["series"]): { direction: "up" | "down" | "flat"; label: string } | null {
  if (series.length < 2) return null;
  const previous = series[series.length - 2].value;
  const latest = series[series.length - 1].value;
  const delta = latest - previous;
  const direction = delta > 0 ? "up" : delta < 0 ? "down" : "flat";
  const amount = previous !== 0
    ? `${Math.abs(delta / previous * 100).toLocaleString(undefined, { maximumFractionDigits: 1 })}%`
    : Math.abs(delta).toLocaleString(undefined, { maximumFractionDigits: 2 });
  return { direction, label: direction === "flat" ? "0" : amount };
}

/** Opens a tracked number's detail drawer: history, evidence and how to refresh it. */
function useOpenMetric(workspaceId: string, canStart: boolean) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  return (metric: WorkspaceAppMetric, event: React.MouseEvent<HTMLButtonElement>) => openMetricDetail({
    workspaceId, metric, canStart, navigate, queryClient, returnFocusTo: event.currentTarget,
  });
}

// The shared Stats formatter keeps units, currency and percent identical to the Stats page.
function metricStat(metric: WorkspaceAppMetric): WorkspaceStatDefinition {
  return {
    value_type: metric.value_type, unit: metric.unit, current_value: metric.value, freshness_status: metric.freshness_status,
  } as WorkspaceStatDefinition;
}

// Where the number comes from and how fresh it is; gaps and schedules live in the drawer.
function metricNote(metric: WorkspaceAppMetric): string {
  return [
    metric.collector_type === "agent_review" ? t("component.workspace_app_view.metrics_assessed") : null,
    metric.updated_at ? relativeTime(metric.updated_at, "") : null,
  ].filter(Boolean).join(" · ");
}

function MetricChange({ series }: { series: WorkspaceAppMetric["series"] | undefined }) {
  const change = metricChange(series || []);
  return change && <small className={`workspace-app-metric__change workspace-app-metric__change--${change.direction}`}
    title={t("component.workspace_app_view.metrics_change")}>
    {change.direction === "up" ? "↑" : change.direction === "down" ? "↓" : "→"} {change.label}
  </small>;
}

function MetricTile({ metric, onOpen }: { metric: WorkspaceAppMetric; onOpen: (event: React.MouseEvent<HTMLButtonElement>) => void }) {
  const stat = metricStat(metric);
  const attention = workspaceStatAttentionPresentation(metric.freshness_status);
  const goal = metric.goal;
  const percent = goal && goal.target !== null && metric.value !== null
    ? Math.round(goalProgressPercent({ current_value: metric.value, target_value: goal.target, baseline_value: goal.baseline ?? 0 }))
    : null;
  const pace = goal?.status === "achieved" ? "achieved" : goal?.pace_status && goal.pace_status !== "unknown" ? goal.pace_status : null;
  const paceTone = pace === "achieved" || pace === "on_track" || pace === "ahead" ? "done" : "attention";
  // How far is left: up to a target above where it started, down to one below it.
  const rising = goal && goal.target !== null ? goal.target >= (goal.baseline ?? 0) : true;
  const remaining = goal && goal.target !== null && metric.value !== null && pace !== "achieved"
    ? rising ? goal.target - metric.value : metric.value - goal.target : null;
  return <li>
    <button type="button" className="workspace-app-metric" onClick={onOpen} aria-haspopup="dialog">
      <span className="workspace-app-metric__label" title={metric.description || undefined}>{metric.title}</span>
      <span className="workspace-app-metric__reading">
        <strong className="workspace-app-metric__value">{formatWorkspaceStatValue(stat, metric.value)}</strong>
        <MetricChange series={metric.series} />
      </span>
      <Sparkline series={metric.series} />
      {goal && goal.target !== null && <span className="workspace-app-metric__goal">
        <span className="workspace-app-metric__track" aria-hidden="true"><span style={{ width: `${percent ?? 0}%` }} /></span>
        <small>
          {t("component.workspace_app_view.metrics_target", { target: formatWorkspaceStatValue(stat, goal.target) })}
          {percent !== null && ` · ${percent}%`}
          {remaining !== null && remaining > 0 && ` · ${t("component.workspace_app_view.metrics_to_go", { amount: formatWorkspaceStatValue(stat, remaining) })}`}
          {pace && <> · <span className={`workspace-app-metric__pace workspace-app-metric__pace--${paceTone}`}>
            {t(`component.workspace_app_view.pace_${pace}`)}</span></>}
        </small>
      </span>}
      {/* One quiet footer line: a problem when there is one, otherwise the source and freshness. */}
      <small className="workspace-app-metric__foot">
        {attention
          ? <><i className={`workspace-app-metric__dot workspace-app-metric__dot--${metricPhaseTone(metric.freshness_status)}`} aria-hidden="true" />{attention.label}</>
          : metricNote(metric)}
      </small>
    </button>
  </li>;
}

/** A number past the headline tiles: one row, the state under its name, the reading on the right.
 * A number still waiting for its first reading keeps its place with a dash. */
function MetricRow({ metric, onOpen }: { metric: WorkspaceAppMetric; onOpen: (event: React.MouseEvent<HTMLButtonElement>) => void }) {
  const waiting = metric.value === null;
  // Before a first reading, only a problem, an assessment under way or a pause says more than the wait.
  const phase = metric.freshness_status;
  const attention = waiting && !ATTENTION_PHASES.has(phase) && phase !== "assessing" && phase !== "paused"
    ? null : workspaceStatAttentionPresentation(phase);
  return <li>
    <button type="button" className="workspace-app-metric-row" onClick={onOpen} aria-haspopup="dialog">
      <span className="workspace-app-metric-row__text">
        <strong>{metric.title}</strong>
        <small className="workspace-app-metric__foot">
          {attention && <i className={`workspace-app-metric__dot workspace-app-metric__dot--${metricPhaseTone(metric.freshness_status)}`} aria-hidden="true" />}
          {attention ? attention.label : waiting ? t("component.workspace_app_view.metrics_waiting_first") : metricNote(metric)}
        </small>
      </span>
      {!waiting && <MetricChange series={metric.series} />}
      <span className={`workspace-app-metric-row__value${waiting ? " is-waiting" : ""}`}>
        {waiting ? "—" : formatWorkspaceStatValue(metricStat(metric), metric.value)}
      </span>
      <IconChevronRight size={14} aria-hidden="true" />
    </button>
  </li>;
}

/** When the next number is read, for the section head. */
function nextReadingLabel(metrics: WorkspaceAppMetric[]): string | null {
  const next = metrics.map((metric) => metric.next_assessment_at).filter((value): value is string => Boolean(value)).sort()[0];
  if (!next) return null;
  const time = new Date(next).toLocaleString(getLocale(), {
    month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone: getPreferredTimeZone(),
  });
  return t("component.workspace_app_view.metrics_next_reading", { time });
}

/** Start tracking audience numbers that an agent assesses from the Workspace's platforms. */
function AudienceMetricsOffer({ workspaceId, templates }: { workspaceId: string; templates: WorkspaceAssessmentTemplate[] }) {
  const queryClient = useQueryClient();
  const toast = useToastStore();
  // Most media Workspaces want these; active users is opt-in.
  const [chosen, setChosen] = useState(() => new Set(templates.filter((item) => item.key !== "active_users").map((item) => item.key)));
  const add = useMutation({
    mutationFn: () => api.workspaces.stats.addAssessedMetrics(workspaceId, { templates: [...chosen], cadence: "daily" }),
    onSuccess: (result) => {
      if (result.schedule_enabled) toast.success(t("component.workspace_app_view.audience_added"));
      else toast.warning(t("component.workspace_app_view.audience_added"), t("component.workspace_app_view.audience_schedule_off"));
      void queryClient.invalidateQueries({ queryKey: ["workspace-app-section", workspaceId] });
    },
    onError: (error: Error) => toast.error(t("component.workspace_app_view.audience_add_failed"), error.message),
  });
  const toggle = (key: string) => setChosen((current) => {
    const next = new Set(current);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });
  return <div className="workspace-app-audience">
    <p><strong>{t("component.workspace_app_view.audience_title")}</strong>{t("component.workspace_app_view.audience_description")}</p>
    <div className="workspace-app-audience__choices" role="group" aria-label={t("component.workspace_app_view.audience_title")}>
      {templates.map((template) => <button key={template.key} type="button" aria-pressed={chosen.has(template.key)}
        onClick={() => toggle(template.key)}>{template.name}</button>)}
    </div>
    <Button variant="outline" size="sm" onClick={() => add.mutate()} disabled={!chosen.size} loading={add.isPending}>
      {t("component.workspace_app_view.audience_add")}
    </Button>
  </div>;
}

function MetricsSection({ workspaceId, section, canStart }: { workspaceId: string; section: WorkspaceAppSection; canStart: boolean }) {
  const query = useSectionData(workspaceId, section, () => 60_000);
  const metrics = (query.data?.block === "metrics" ? query.data.items : []) as WorkspaceAppMetric[];
  const suggestions = query.data?.block === "metrics" ? query.data.suggestions || [] : [];
  const offer = !metrics.length && canStart && suggestions.length > 0;
  const title = section.title || t("component.workspace_app_view.metrics_title");
  const openMetric = useOpenMetric(workspaceId, canStart);
  // A few headline numbers with a reading get a tile; every other number is a
  // row below them: ones that need someone first, then the rest of the
  // readings, then ones still waiting for a first reading.
  const headline = section.limit ?? DEFAULT_HEADLINE_METRICS;
  const tiles = metrics.filter((metric) => metric.value !== null).slice(0, headline);
  const rank = (metric: WorkspaceAppMetric) => ATTENTION_PHASES.has(metric.freshness_status) ? 0 : metric.value !== null ? 1 : 2;
  const rows = metrics.filter((metric) => !tiles.includes(metric)).sort((a, b) => rank(a) - rank(b));
  if (query.isError) {
    return <section className="workspace-app-view__section workspace-app-section" aria-label={title}>
      <SectionHead title={title} />
      <SectionError onRetry={() => void query.refetch()} />
    </section>;
  }
  // The default page shows tracked data only once the Workspace tracks
  // something, or when it is a media Workspace that can start tracking.
  if (section.implicit && !metrics.length && !offer) return null;
  return <section className="workspace-app-view__section workspace-app-section" aria-label={title} aria-busy={query.isPending || undefined}>
    <SectionHead title={title} detail={metrics.length ? nextReadingLabel(metrics) : null} />
    {query.isPending ? <div className="workspace-app-section__skeleton" aria-hidden="true"><span /><span /></div>
      : offer ? <AudienceMetricsOffer workspaceId={workspaceId} templates={suggestions} />
      : !metrics.length ? <p className="workspace-app-section__note">{t("component.workspace_app_view.metrics_empty")}</p>
        : <>
          {tiles.length > 0 && <ul className="workspace-app-metrics">{tiles.map((metric) => <MetricTile key={`${metric.kind}:${metric.id}`}
            metric={metric} onOpen={(event) => openMetric(metric, event)} />)}</ul>}
          {rows.length > 0 && <ul className="workspace-app-metric-rows">{rows.map((metric) => <MetricRow key={`${metric.kind}:${metric.id}`}
            metric={metric} onOpen={(event) => openMetric(metric, event)} />)}</ul>}
        </>}
  </section>;
}

/** A decision's summary as one plain line; the chat card keeps the formatting. */
function plainSummary(value: string | null | undefined): string {
  return formatUserFacingText(value)
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1$2")
    .replace(/`([^`]+)`/g, "$1")
    // A summary cut mid-markup leaves unclosed markers behind.
    .replace(/\*\*|`/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

/** A confirmation ("the file was delivered") is confirmed, not accepted. */
function queuePrimaryLabel(option: string): string {
  const normalized = option.toLowerCase().replace(/[-\s]+/g, "_");
  if (normalized === "confirm") return t("component.workflow_run.action.confirm");
  if (normalized === "done") return t("component.workflow_run.action.done");
  return t("component.workflow_review.accept");
}

/** A pending decision. A workflow review can be accepted here, with a glimpse of
 * what is waiting; anything else, and every change request, opens its card in the App. */
function DecisionRow({ item, workspaceId }: { item: WorkspaceAppQueueItem; workspaceId: string }) {
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const navigate = useNavigate();
  const flowsAccess = usePreviewFeatureAccess("flows");
  const [opened, setOpened] = useState(false);
  const run = item.workflow_run;
  const open = () => run ? navigate(workflowRunPath(run.workflow_id, run.run_id, workspaceId)) : setOpened(true);
  const accept = useMutation({
    mutationFn: () => api.workspaces.chat.resolveAction(workspaceId, item.message_id, item.primary_option!),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["workspace-app-section", workspaceId] }),
    onError: (error: Error) => toast.error(t("component.workspace_app_view.queue_accept_failed"), error.message),
  });
  const title = plainSummary(item.summary) || t("component.workspace_app_view.queue_untitled");
  const previews = item.previews || [];
  const dialog = opened && <Suspense fallback={null}>
    <WorkspaceAppDecisionDialog workspaceId={workspaceId} messageId={item.message_id} title={title} onClose={() => setOpened(false)} />
  </Suspense>;
  if (!item.primary_option) {
    return <li className="workspace-app-notices__row">
      <button type="button" onClick={open} disabled={Boolean(run) && !flowsAccess.enabled}>
        {/* An Agent asking about files shows a few of them; answering stays on its chat card. */}
        {previews.length > 0
          ? <span className="workspace-app-notices__previews" aria-hidden="true">
            {previews.map((preview) => <span key={preview.document_id} className="workspace-app-notices__preview">
              <OutputThumbnail file={{ name: preview.name, document_id: preview.document_id } as WorkspaceAppOutputFile} />
            </span>)}
          </span>
          : <span className="workspace-app-group__icon workspace-app-group__icon--attention" aria-hidden="true"><IconWarning size={16} /></span>}
        <span className="workspace-app-group__text">
          <strong>{title}</strong>
          <small>{relativeTime(item.created_at, "")}</small>
        </span>
        <span className="workspace-app-notices__open">{t("component.workspace_app_view.queue_open")}<IconArrowRight size={14} aria-hidden="true" /></span>
      </button>
      {dialog}
    </li>;
  }
  return <li className="workspace-app-notices__row workspace-app-notices__row--review">
    <button type="button" className="workspace-app-notices__review-main" onClick={open}>
      {previews.length > 0
        ? <span className="workspace-app-notices__previews" aria-hidden="true">
          {previews.map((preview) => <span key={preview.document_id} className="workspace-app-notices__preview">
            <OutputThumbnail file={{ name: preview.name, document_id: preview.document_id } as WorkspaceAppOutputFile} />
          </span>)}
        </span>
        : <span className="workspace-app-group__icon workspace-app-group__icon--attention" aria-hidden="true"><IconWarning size={16} /></span>}
      <span className="workspace-app-group__text">
        <strong>{title}</strong>
        <small>{relativeTime(item.created_at, "")}</small>
      </span>
    </button>
    <span className="workspace-app-notices__actions">
      <Button size="sm" variant="primary" loading={accept.isPending} disabled={accept.isPending || accept.isSuccess}
        onClick={() => accept.mutate()}>
        {queuePrimaryLabel(item.primary_option)}
      </Button>
      <Button size="sm" variant="outline" onClick={open}>
        {item.has_changes_option ? t("component.workspace_app_view.queue_review") : t("component.workspace_app_view.queue_open")}
      </Button>
    </span>
    {dialog}
  </li>;
}

/** One broken section hides itself instead of taking the App page down. */
class SectionBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: { componentStack: string }) {
    captureClientError(error, {
      handled: true, mechanism: "react.error_boundary", boundary: "WorkspaceAppSection", componentStack: info.componentStack,
    });
  }

  render() {
    return this.state.failed ? null : this.props.children;
  }
}

type NoticeRow = { key: string; urgent: boolean; node: ReactNode };

type BlockProps = {
  workspaceId: string;
  section: WorkspaceAppSection;
  /** Whether the viewer may start, refresh or record from the App right now. */
  canStart: boolean;
  /** The App entry that shows this run's result in place, when the App has one. */
  runAction?: (run: WorkspaceAppOutputRun) => string | null;
};

/**
 * How each block of the server's catalog renders, kept complete by the
 * Record type. Notice blocks give rows that the notices zone merges into one
 * card; ``actions`` is laid out by the page as its functions zone; the rest
 * render as their own section in their zone.
 */
type BlockRenderer =
  | { kind: "notice"; refetch: (data: unknown) => number | false; rows: (data: WorkspaceAppSectionData, workspaceId: string) => NoticeRow[] }
  | { kind: "page" }
  | { kind: "section"; render: (props: BlockProps) => JSX.Element | null };

const BLOCK_RENDERERS: Record<WorkspaceAppSectionBlock, BlockRenderer> = {
  connections: {
    kind: "notice",
    refetch: (data) => ((data as { items?: WorkspaceConnectionRequirement[] } | undefined)?.items || []).some((item) => item.required) ? 30_000 : false,
    // A platform with no channel to attach is optional context, not something the person owes.
    rows: (data, workspaceId) => data.block !== "connections" ? [] : data.items.filter((requirement) => requirement.required || requirement.setup_kind !== "platform_access").map((requirement) => ({
      key: `connection:${requirement.key}`, urgent: requirement.required,
      node: <IntegrationConnectRow key={requirement.key} workspaceId={workspaceId} requirement={requirement} />,
    })),
  },
  queue: {
    kind: "notice",
    refetch: () => 30_000,
    rows: (data, workspaceId) => data.block !== "queue" ? [] : data.items.map((item) => ({
      key: `decision:${item.key}`, urgent: true, node: <DecisionRow key={item.key} item={item} workspaceId={workspaceId} />,
    })),
  },
  actions: { kind: "page" },
  metrics: { kind: "section", render: ({ workspaceId, section, canStart }) => <MetricsSection workspaceId={workspaceId} section={section} canStart={canStart} /> },
  outputs: { kind: "section", render: ({ workspaceId, section, runAction }) => <OutputsSection workspaceId={workspaceId} section={section} runAction={runAction} /> },
};

const NOTICE_PREVIEW = 3;

/**
 * Zone 1: what needs the person, merged into one card at the top: missing
 * connections and pending decisions. Optional connections wait behind a line.
 * Hidden when nothing needs anyone.
 */
export function NoticesZone({ workspaceId, sections, canConnect }: {
  workspaceId: string;
  sections: WorkspaceAppSection[];
  /** Connecting follows permission, not a running simulation that pauses actions. */
  canConnect: boolean;
}) {
  const visible = usePageVisible();
  const notices = sections.filter((section) => BLOCK_RENDERERS[section.block].kind === "notice"
    && (section.block !== "connections" || canConnect));
  const draftId = useWorkspaceAppDraftId();
  const results = useQueries({ queries: notices.map((section) => {
    const renderer = BLOCK_RENDERERS[section.block] as Extract<BlockRenderer, { kind: "notice" }>;
    return sectionQueryOptions(workspaceId, section, renderer.refetch, draftId, visible);
  }) });
  const [expanded, setExpanded] = useState(false);
  const [showOptional, setShowOptional] = useState(false);
  const rows = notices.flatMap((section, index) => {
    const data = results[index]?.data;
    const renderer = BLOCK_RENDERERS[section.block];
    return data && renderer.kind === "notice" ? renderer.rows(data, workspaceId) : [];
  });
  const urgent = rows.filter((row) => row.urgent);
  const optional = rows.filter((row) => !row.urgent);
  if (!urgent.length && !optional.length) return null;
  const title = t("component.workspace_app_view.notices_title");
  const shown = expanded ? urgent : urgent.slice(0, NOTICE_PREVIEW);
  const optionalToggle = optional.length > 0 && <button type="button" className="workspace-app-notices__optional"
    onClick={() => setShowOptional((value) => !value)} aria-expanded={showOptional}>
    {t("component.workspace_app_view.connections_optional", { count: optional.length })}
  </button>;
  return <section className="workspace-app-view__section workspace-app-zone workspace-app-zone--notices" aria-label={title}>
    {urgent.length > 0 ? <div className="workspace-app-notices">
      <div className="workspace-app-notices__head">
        <IconBell size={15} aria-hidden="true" />
        <h3>{title} · {urgent.length}</h3>
        {urgent.length > NOTICE_PREVIEW && <button type="button" className="workspace-app-section__more" onClick={() => setExpanded((value) => !value)}
          aria-expanded={expanded}>
          {expanded ? t("component.workspace_app_view.metrics_show_less") : t("component.workspace_app_view.metrics_show_all", { count: urgent.length })}
        </button>}
      </div>
      <ul className="workspace-app-notices__list">{shown.map((row) => row.node)}</ul>
      {optionalToggle}
    </div> : optionalToggle}
    {showOptional && <ul className="workspace-app-group workspace-app-notices__optional-list">{optional.map((row) => row.node)}</ul>}
  </section>;
}

/** Which zone renders a block: the page itself, the notices card, or its own section. */
export function blockRenderKind(block: WorkspaceAppSectionBlock): BlockRenderer["kind"] {
  return BLOCK_RENDERERS[block].kind;
}

export default function WorkspaceAppSectionBlock({ workspaceId, section, canStart = false, runAction }: {
  workspaceId: string;
  section: WorkspaceAppSection;
  canStart?: boolean;
  runAction?: (run: WorkspaceAppOutputRun) => string | null;
}) {
  const renderer = BLOCK_RENDERERS[section.block];
  if (renderer.kind !== "section") return null;
  const Render = renderer.render;
  return <SectionBoundary><Render workspaceId={workspaceId} section={section} canStart={canStart} runAction={runAction} /></SectionBoundary>;
}
