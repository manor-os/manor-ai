/**
 * RunProduction — a Workflow run shown as the thing it is making.
 *
 * A Workflow that declares `production` on its trigger (see
 * workflow_production.py) is shown as its stages, how far along it is and how
 * long is left, and its units (scenes, slides, sections) on a timeline, each
 * becoming its clip as it is made. Picking a unit opens it to watch, adjust,
 * regenerate or switch versions. Nothing here is specific to one kind of video:
 * the stages, the unit's name, its fields and the lanes under it all come from
 * the Workflow's configuration.
 */
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { api, resolveDisplayMediaUrl, type MediaJob } from "../../lib/api";
import { t } from "../../lib/i18n";
import type { ResultFileMetadata } from "../../lib/resultFiles";
import GeneratedFileCard from "../GeneratedFileCard";
import Button from "./Button";
import Textarea from "./Textarea";
import MediaJobCard, { phaseOf, remainingLabel } from "./MediaJobCard";
import { clipLines } from "./RunMediaClips";
import "./RunProduction.css";
import { displayReadInterval } from "../../lib/readRefreshPolicy";
import { usePageVisible } from "../../lib/usePageVisible";
import { scheduleMediaRunRead } from "../../lib/mediaRunReadScheduler";
import { runPercent, stageShare, stageStates, type StepState } from "../../lib/runProductionProgress";

export type RunProductionConfig = {
  /** What the run makes: its units' media jobs are of this kind and its finished whole plays as one. */
  media?: "video" | "audio" | "image";
  unit: { name: string };
  lanes: Array<{ key: string; label: string }>;
  stages: Array<{ label: string; steps: string[]; counts_units: boolean; weight: number }>;
  units: Array<{ id: string; title: string; duration_s: number | null; lanes: Record<string, string> }>;
  /** The finished whole, once the run has saved it to Knowledge. */
  final?: { document_id: string; name: string };
  /** Units the run rendered itself (a still end card): finished without a media job. */
  units_made_locally?: Array<{ unit_id: string; document_id: string; name: string }>;
  /** How a finished result is changed (a later version starts from this step). */
  revise?: { from_step: string[]; notes_input: string; versions_input?: string };
};

/** How far into making one clip a job is, when its model gives no percentage. */
const PHASE_SHARE: Record<string, number> = { queued: 0, submitting: 0.1, generating: 0.5, saving: 0.9, done: 1 };

/** The newest finished version of a unit's clip: what plays for it. */
function latestFinished(line: { root: MediaJob; latest: MediaJob }, jobs: MediaJob[]): MediaJob | undefined {
  if (line.latest.status === "completed") return line.latest;
  return [...jobs].reverse().find((job) => (job.lineage_root || job.id) === line.root.id && job.status === "completed");
}

// Eases a shown number to its new value, so progress counts up instead of jumping.
function useCountUp(target: number): number {
  const [shown, setShown] = useState(target);
  const from = useRef(target);
  useEffect(() => {
    const start = from.current;
    if (start === target || typeof window === "undefined" || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
      from.current = target;
      setShown(target);
      return;
    }
    const began = performance.now();
    let frame = 0;
    const step = (now: number) => {
      const progress = Math.min(1, (now - began) / 600);
      const value = Math.round(start + (target - start) * (1 - (1 - progress) ** 3));
      from.current = value;
      setShown(value);
      if (progress < 1) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [target]);
  return shown;
}

function ClipFrame({ url }: { url: string }) {
  const [src, setSrc] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    let revoke = () => {};
    resolveDisplayMediaUrl(url).then((resolved) => {
      revoke = resolved.revoke;
      if (!cancelled) setSrc(resolved.url);
      else resolved.revoke();
    }).catch(() => undefined);
    return () => { cancelled = true; revoke(); };
  }, [url]);
  return src ? <video src={`${src}#t=0.5`} muted playsInline preload="metadata" aria-hidden="true" /> : null;
}

type PreviewUnit = { id: string; url: string | null; seconds: number; text: string };

/**
 * The video so far, played through in unit order: each finished unit's clip,
 * and for a unit not made yet its planned text for as long as it will last.
 */
function PreviewPlayer({ units, current, onCurrent }: {
  units: PreviewUnit[];
  current: number;
  onCurrent: (index: number) => void;
}) {
  const [playing, setPlaying] = useState(false);
  const [src, setSrc] = useState<string | null>(null);
  const unit = units[current];
  const videoRef = useRef<HTMLVideoElement | null>(null);
  useEffect(() => {
    let cancelled = false;
    let revoke = () => {};
    setSrc(null);
    if (unit?.url) {
      resolveDisplayMediaUrl(unit.url).then((resolved) => {
        revoke = resolved.revoke;
        if (!cancelled) setSrc(resolved.url);
        else resolved.revoke();
      }).catch(() => undefined);
    }
    return () => { cancelled = true; revoke(); };
  }, [unit?.url]);
  const advance = () => {
    if (current + 1 < units.length) onCurrent(current + 1);
    else { setPlaying(false); onCurrent(0); }
  };
  // A unit without its clip yet holds its planned text for its length.
  useEffect(() => {
    if (!playing || !unit || unit.url) return;
    const timer = window.setTimeout(advance, Math.max(2, Math.min(unit.seconds, 15)) * 1000);
    return () => window.clearTimeout(timer);
  });
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    if (playing) void video.play().catch(() => setPlaying(false));
    else video.pause();
  }, [playing, src]);
  if (!unit) return null;
  return <div className="run-production__player">
    <div className="run-production__screen">
      {unit.url && src
        ? <video ref={videoRef} src={src} playsInline preload="auto" onEnded={advance} />
        : <p className="run-production__screen-text">{unit.text}</p>}
    </div>
    <div className="run-production__player-bar">
      <button type="button" onClick={() => setPlaying((value) => !value)}>
        {playing ? t("component.run_production.pause") : t("component.run_production.play_so_far")}
      </button>
      <span className="mono">{current + 1} / {units.length}</span>
      <span>{units.filter((item) => item.url).length < units.length ? t("component.run_production.preview_hint") : ""}</span>
    </div>
  </div>;
}

/**
 * The number a unit id ends with ("scene-03", "block_003", "B03" are all 3), to
 * match clips named another way; a revision suffix ("scene-03-r01") is not it.
 */
function unitNumber(id: string): number | null {
  const match = /(\d+)\D*$/.exec(id.replace(/\.[a-z0-9]+$/i, "").replace(/-(r|take)\d+$/i, ""));
  return match ? Number(match[1]) : null;
}

/**
 * Which planned unit a clip is (as the server's clip_unit_id): its recorded unit id, the unit id its
 * saved name spells out as a whole word (`song-S05-take2`), or else the unit's number.
 */
function clipUnitFor(job: MediaJob, unitIds: string[]): string | null {
  const key = clipUnitKey(job);
  if (unitIds.includes(key)) return key;
  const named = unitIds.filter((id) => new RegExp(
    `(?<![A-Za-z0-9])${id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?![A-Za-z0-9])`, "i").test(key));
  if (named.length === 1) return named[0];
  const number = unitNumber(key);
  const numbered = unitIds.filter((id) => number != null && unitNumber(id) === number);
  return numbered.length === 1 ? numbered[0] : null;
}

/** Which unit a clip is: its unit id, else the name it was saved under. */
function clipUnitKey(job: MediaJob): string {
  if (job.unit_id) return job.unit_id;
  const name = String(job.params?.output_name || "").split("/").pop() || "";
  return name.replace(/\.[a-z0-9]+$/i, "").replace(/-r\d+$/i, "");
}

/**
 * Changing the finished whole: say what should change, or bring in the unit
 * versions picked since. Either makes the next version; this one is kept.
 */
function RevisePanel({ runId, changed, unitVersions, onStarted }: {
  runId: string;
  changed: number;
  unitVersions: Record<string, string>;
  onStarted: (runId: string) => void;
}) {
  const [notes, setNotes] = useState("");
  const revise = useMutation({
    mutationFn: (withNotes: boolean) => api.workflows.reviseRun(runId, {
      notes: withNotes ? notes.trim() : "", unit_versions: unitVersions,
    }),
    onSuccess: (created) => { setNotes(""); onStarted(created.id); },
  });
  return <div className="run-production__revise">
    {changed > 0 && <div className="run-production__changed">
      <span>{t("component.run_production.units_changed", { count: changed })}</span>
      <Button size="sm" variant="primary" loading={revise.isPending && !notes.trim()} disabled={revise.isPending}
        onClick={() => revise.mutate(false)}>{t("component.run_production.update_whole")}</Button>
    </div>}
    <Textarea
      value={notes}
      rows={2}
      placeholder={t("component.run_production.revise_placeholder")}
      ariaLabel={t("component.run_production.revise_label")}
      onChange={(event) => setNotes(event.target.value)}
    />
    <div className="run-production__revise-actions">
      <Button size="sm" variant={changed > 0 ? "outline" : "primary"} loading={revise.isPending && Boolean(notes.trim())}
        disabled={!notes.trim() || revise.isPending} onClick={() => revise.mutate(true)}>
        {t("component.run_production.revise")}
      </Button>
      <small>{t("component.run_production.revise_hint")}</small>
    </div>
    {revise.error && <p role="alert" className="run-production__error">
      {revise.error instanceof Error ? revise.error.message : t("component.run_production.revise_failed")}
    </p>}
  </div>;
}

function combineFamilyClips(results: Array<{ data?: MediaJob[] }>): MediaJob[] {
  return results.flatMap((query) => query.data || [])
    .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
}

type FamilyRun = { id: string; status: string; started_at?: string | null; created_at?: string | null; completed_at?: string | null };

export default function RunProduction({
  runId, config, steps, currentStepId, status, startedAt, finalResult, canRevise, onOpenRun,
}: {
  runId: string;
  config: RunProductionConfig;
  steps: StepState[];
  currentStepId: string | null;
  status: string;
  startedAt?: string | null;
  /** The finished whole (a video, a song), once the run has made it: the view leads with it. */
  finalResult?: ResultFileMetadata & { reference: string } | null;
  /** The person may change this run's result (its Workflow declares how, and they control the run). */
  canRevise?: boolean;
  /** Show another version (run) of this result. */
  onOpenRun?: (runId: string) => void;
}) {
  const finished = status === "completed";
  // A paused run (waiting on media, a timer or a review) still reads as working;
  // only its own reads slow down.
  const running = !["completed", "failed", "cancelled"].includes(status);
  const paused = status === "paused";
  const advancing = running && !paused;
  const visible = usePageVisible();
  const queryClient = useQueryClient();
  // A stopped run's panel already says so; how far it got shows in its stages.
  const stopped = status === "failed" || status === "cancelled";
  // Every version of this result is a run of one family; a later version reuses
  // the units it did not change, so their clips come from the earlier runs.
  const family = useQuery({
    queryKey: ["workflow-run-family", runId],
    queryFn: ({ signal }) => api.workflows.getRunFamily(runId, AbortSignal.any([signal, AbortSignal.timeout(15_000)])) as Promise<FamilyRun[]>,
    enabled: visible,
    staleTime: advancing ? 0 : 30_000,
    // A completed parent can later gain a retried family member. Reconcile its
    // membership at a low rate so that member becomes eligible for a read.
    refetchInterval: (query) => displayReadInterval(query.state, advancing ? 20_000 : paused ? 60_000 : 120_000),
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
  });
  const familyIds = useMemo(() => {
    const ids = (family.data || []).map((item) => item.id);
    return ids.includes(runId) ? ids : [...ids, runId];
  }, [family.data, runId]);
  const familyById = useMemo(() => new Map((family.data || []).map((member) => [member.id, member])), [family.data]);
  const terminalReconcileMs = familyIds.length > 12 ? 300_000 : familyIds.length > 4 ? 180_000 : 120_000;
  const eventTimer = useRef<number | null>(null);
  const pendingEventRuns = useRef(new Set<string>());
  const pendingUnknownEvent = useRef(false);
  const lastUnknownReconcileAt = useRef(0);
  // Per-run keys let this family view share its current run's read with
  // RunMediaClips instead of maintaining a second aggregate cache.
  const data = useQueries({
    combine: combineFamilyClips,
    queries: familyIds.map((id) => ({
      queryKey: ["media-jobs", "run", id],
      queryFn: ({ signal }: { signal: AbortSignal }) => scheduleMediaRunRead(
        signal,
        () => api.mediaJobs.forRun(id, AbortSignal.any([signal, AbortSignal.timeout(15_000)])),
        id === runId || ["pending", "queued", "running"].includes(familyById.get(id)?.status || "") ? 0 : 1,
      ),
      enabled: visible,
      refetchInterval: (query: { state: { data?: MediaJob[]; status?: string; error?: unknown } }) => {
        const mediaActive = query.state.data?.some((job) => ["pending", "processing"].includes(job.status));
        const runActive = ["pending", "queued", "running"].includes(familyById.get(id)?.status || "");
        const current = id === runId;
        const normal = mediaActive || (current && advancing) || runActive
          ? 15_000
          : current && paused ? 60_000 : terminalReconcileMs;
        return displayReadInterval(query.state, normal);
      },
      refetchIntervalInBackground: false,
      refetchOnWindowFocus: true,
    })),
  });
  // Event subscription reads the latest values through refs so a re-render
  // (the elapsed clock ticks every second) never clears a pending flush.
  const eventScope = useRef({ known: new Set<string>(), familyIds, runId, visible });
  eventScope.current = { known: new Set(data.map((job) => job.id)), familyIds, runId, visible };
  // Socket messages can arrive in bursts. Coalesce a matching family update;
  // messages without a run id only cause one bounded current-run reconciliation.
  useEffect(() => {
    const flush = () => {
      eventTimer.current = null;
      const ids = [...pendingEventRuns.current];
      pendingEventRuns.current.clear();
      const { runId } = eventScope.current;
      for (const id of ids) void queryClient.invalidateQueries({ queryKey: ["media-jobs", "run", id] }, { cancelRefetch: false });
      if (pendingUnknownEvent.current) {
        pendingUnknownEvent.current = false;
        void queryClient.invalidateQueries({ queryKey: ["workflow-run-family", runId] }, { cancelRefetch: false });
        void queryClient.invalidateQueries({ queryKey: ["media-jobs", "run", runId] }, { cancelRefetch: false });
      }
    };
    const schedule = () => {
      if (eventTimer.current !== null) return;
      eventTimer.current = window.setTimeout(flush, 500);
    };
    const onEvent = (event: Event) => {
      const update = (event as CustomEvent<Partial<MediaJob>>).detail;
      const { known, familyIds, visible } = eventScope.current;
      if (!visible || !update) return;
      if (update.run_id && update.id && !known.has(update.id) && familyIds.includes(update.run_id)) {
        pendingEventRuns.current.add(update.run_id);
        schedule();
        return;
      }
      if (!update.run_id && Date.now() - lastUnknownReconcileAt.current >= 15_000) {
        lastUnknownReconcileAt.current = Date.now();
        pendingUnknownEvent.current = true;
        schedule();
      }
    };
    window.addEventListener("manor:media-job-update", onEvent);
    return () => {
      window.removeEventListener("manor:media-job-update", onEvent);
      if (eventTimer.current !== null) window.clearTimeout(eventTimer.current);
      eventTimer.current = null;
    };
  }, [queryClient]);

  const media = config.media || "video";
  const lines = useMemo(() => clipLines(data.filter((job) => job.kind === media)), [data, media]);
  // Units come from the run's plan; a clip finds its unit by id, by the number
  // its id ends with, or else by order. The newest clip of a unit wins.
  const units = useMemo(() => {
    const oldestFirst = [...lines].sort((a, b) => String(a.root.created_at || "").localeCompare(String(b.root.created_at || "")));
    const planned = config.units.length ? config.units : oldestFirst.map((line, index) => ({
      id: clipUnitKey(line.root) || String(index + 1), title: "", duration_s: line.root.duration_seconds || null,
      lanes: {} as Record<string, string>,
    }));
    const unitIds = planned.map((unit) => unit.id);
    const byUnit = new Map<string, (typeof lines)[number]>();
    for (const line of oldestFirst) {
      const id = clipUnitFor(line.root, unitIds);
      if (id) byUnit.set(id, line);
    }
    const keyed = oldestFirst.some((line) => clipUnitKey(line.root));
    return planned.map((unit, index) => ({
      ...unit, line: byUnit.get(unit.id) || (!keyed ? oldestFirst[index] : undefined),
    }));
  }, [config.units, lines]);
  const finishedIds = new Set(data.filter((job) => job.status === "completed").map((job) => job.lineage_root || job.id));
  const unitShare = (line?: { root: MediaJob; latest: MediaJob }) => !line ? 0
    : finishedIds.has(line.root.id) ? 1 : PHASE_SHARE[phaseOf(line.latest)] ?? 0;

  const states = stageStates(config.stages, steps, currentStepId, status);
  // A unit with no media of its own (a song's section, made in one go with the
  // track) is made once the run is, or once the stage that makes units is done.
  const unitsMade = finished || states.some((state, index) => config.stages[index].counts_units && state === "done");
  const madeLocally = new Set((config.units_made_locally || []).map((item) => item.unit_id));
  const unitDone = (unit: { id: string; line?: { root: MediaJob } }) =>
    unit.line ? finishedIds.has(unit.line.root.id) || madeLocally.has(unit.id) : unitsMade || madeLocally.has(unit.id);
  const doneUnits = units.filter(unitDone).length;
  const unitStarts = units.reduce<number[]>((starts, unit, index) =>
    [...starts, index ? starts[index - 1] + (units[index - 1].duration_s || 0) : 0], []);
  const [seek, setSeek] = useState<{ at: number; n: number } | null>(null);
  const unitsShare = (index: number) => config.stages[index].counts_units && units.length
    ? units.reduce((sum, unit) => sum + (madeLocally.has(unit.id) ? 1 : unitShare(unit.line)), 0) / units.length
    : null;
  const shares = config.stages.map((stage, index) => stageShare(stage, states[index], steps, unitsShare(index)));
  const percent = finished ? 100 : runPercent(config.stages, shares);

  // Time left while units are being made: the slowest clip in flight, plus the
  // units not started at the pace finished clips took, as many at once as now.
  const inFlight = units.filter((unit) => unit.line && !finishedIds.has(unit.line.root.id) && unit.line.latest.status !== "failed");
  const notStarted = units.filter((unit) => !unit.line && !madeLocally.has(unit.id)).length;
  const took = (data || []).filter((job) => job.status === "completed" && job.started_at && job.completed_at)
    .map((job) => (new Date(job.completed_at!).getTime() - new Date(job.started_at!).getTime()) / 1000);
  const pace = took.length ? took.reduce((a, b) => a + b, 0) / took.length : null;
  const activeIndex = states.findIndex((state, index) => state === "active" && config.stages[index].counts_units);
  const activeCounts = activeIndex >= 0;
  // The estimate covers making the units only; the checks and reviews after
  // them are named instead of being left out of a whole-run "time left".
  const nextStage = activeCounts ? config.stages.slice(activeIndex + 1).find((_, offset) => states[activeIndex + 1 + offset] !== "done") : undefined;
  const remaining = running && activeCounts && (inFlight.length || notStarted)
    ? Math.max(0, ...inFlight.map((unit) => unit.line!.latest.eta_seconds ?? pace ?? 0))
      + (notStarted && pace ? Math.ceil(notStarted / Math.max(1, inFlight.length)) * pace : 0)
    : null;

  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);
  const elapsed = startedAt ? Math.max(0, (now - new Date(startedAt).getTime()) / 1000) : null;

  const [selected, setSelected] = useState<string | null>(null);
  const picked = units.find((unit) => unit.id === selected) || null;
  // The opened unit keeps the snapshot it opened with; its card follows the clip itself.
  const opened = useRef(new Map<string, string>());
  if (picked?.line && !opened.current.has(picked.line.root.id)) {
    opened.current.set(picked.line.root.id, JSON.stringify(picked.line.latest));
  }
  const totalSeconds = units.reduce((sum, unit) => sum + (unit.duration_s || 0), 0);
  const [playhead, setPlayhead] = useState(0);
  const preview: PreviewUnit[] = units.map((unit) => ({
    id: unit.id,
    url: unit.line && finishedIds.has(unit.line.root.id) ? latestFinished(unit.line, data || [])?.result_url || null : null,
    seconds: unit.duration_s || 5,
    text: [unit.title, ...config.lanes.map((lane) => unit.lanes[lane.key])].filter(Boolean).join(" — "),
  }));

  // Versions of the whole: the family's finished runs, oldest first, plus one in the making.
  const familyRuns = [...(family.data || [])].sort((a, b) =>
    String(a.started_at || a.created_at || "").localeCompare(String(b.started_at || b.created_at || "")));
  const versions = familyRuns.filter((item) => item.status === "completed" || item.id === runId
    || ["pending", "running", "paused"].includes(item.status));
  const thisRun = familyRuns.find((item) => item.id === runId);
  // A unit whose chosen clip is newer than this version was made changed after it.
  const chosen = units.map((unit) => ({ unit, job: unit.line ? latestFinished(unit.line, data || []) : undefined }));
  const madeAt = thisRun?.completed_at ? new Date(thisRun.completed_at).getTime() : null;
  const changedUnits = madeAt == null ? [] : chosen.filter(({ job }) => job?.created_at && new Date(job.created_at).getTime() > madeAt);
  // A cancelled later attempt made nothing, so it does not take the place of this version.
  const standing = familyRuns.filter((item) => item.status !== "cancelled" || item.id === runId);
  const isLatest = !standing.length || standing[standing.length - 1].id === runId;
  const reviseOpen = Boolean(finished && canRevise && config.revise && isLatest);

  const shownPercent = useCountUp(percent);

  return <section className="run-production" data-running={running || undefined} aria-label={t("component.run_production.title")}>
    {finalResult
      ? <div className="run-production__final"><GeneratedFileCard file={finalResult} seek={seek} /></div>
      : units.length > 0 && preview.some((unit) => unit.url) && <PreviewPlayer
        units={preview} current={Math.min(playhead, preview.length - 1)}
        onCurrent={(index) => { setPlayhead(index); setSelected(null); }}
      />}
    {versions.length > 1 && <div className="run-production__versions" role="group" aria-label={t("component.run_production.versions")}>
      {versions.map((item, index) => <button
        key={item.id} type="button" aria-pressed={item.id === runId}
        onClick={() => item.id !== runId && onOpenRun?.(item.id)}
      >
        {t("component.video_card.version", { n: index + 1 })}{item.status !== "completed" ? " …" : ""}
      </button>)}
    </div>}
    {reviseOpen && <RevisePanel
      runId={runId}
      changed={changedUnits.length}
      unitVersions={Object.fromEntries(chosen.filter(({ job }) => job).map(({ unit, job }) => [unit.id, job!.id]))}
      onStarted={(id) => onOpenRun?.(id)}
    />}
    {!stopped && <div className="run-production__head">
      <strong className="mono">{shownPercent}%</strong>
      <span>{finished ? t("component.run_production.finished")
        : remaining != null && remaining > 0
          ? `${config.unit.name}: ${remainingLabel(remaining)}${nextStage ? ` · ${t("component.run_production.then_stage", { stage: nextStage.label })}` : ""}`
          : status === "queued" ? t("component.workspace_app_view.queued")
            : running ? t("component.run_production.working") : ""}</span>
      {elapsed != null && running && <span className="mono run-production__elapsed">{Math.floor(elapsed / 60)}:{String(Math.floor(elapsed % 60)).padStart(2, "0")}</span>}
    </div>}
    <ol className="run-production__stages">
      {config.stages.map((stage, index) => <li key={stage.label} data-state={states[index]} style={{ flexGrow: stage.weight }}>
        <span className="run-production__bar"><span style={{ width: `${Math.round(shares[index] * 100)}%` }} /></span>
        <span className="run-production__stage-label">{stage.label}</span>
        <span className="sr-only">{t(`component.run_production.stage_${states[index]}`)}</span>
      </li>)}
    </ol>

    {units.length > 0 && <>
      <div className="workspace-app-flow__files-heading">
        <h4>{config.unit.name}</h4>
        <span className="mono">{doneUnits} / {units.length}{totalSeconds ? ` · ${Math.round(totalSeconds)}s` : ""}</span>
      </div>
      <div className="run-production__timeline" role="listbox" aria-label={config.unit.name}>
        {units.map((unit, index) => {
          const line = unit.line;
          const done = unitDone(unit);
          const failed = line?.latest.status === "failed";
          const share = unitShare(line);
          const state = done ? "done" : failed ? "failed" : line ? "making" : "planned";
          return <button
            key={unit.id}
            type="button"
            role="option"
            aria-selected={selected === unit.id}
            className="run-production__unit"
            data-state={state}
            data-playing={!finalResult && index === playhead || undefined}
            // Every part is the same size; its length is written under it.
            style={{ flexGrow: 1, "--unit-index": index } as CSSProperties}
            onClick={() => {
              setPlayhead(index);
              setSelected(selected === unit.id ? null : unit.id);
              // A section of a finished track plays from where it starts.
              if (finalResult && media !== "video") setSeek((last) => ({ at: unitStarts[index], n: (last?.n || 0) + 1 }));
            }}
          >
            <span className="run-production__frame">
              {done && line?.latest.result_url ? <ClipFrame url={line.latest.result_url} />
                : <span className="run-production__sketch">{unit.title || unit.lanes[config.lanes[0]?.key] || ""}</span>}
              <span className="run-production__index mono">{index + 1}</span>
              {state === "making" && <span className="run-production__unit-bar"><span style={{ width: `${Math.max(6, share * 100)}%` }} /></span>}
            </span>
            <span className="run-production__unit-state">{done ? t("component.video_card.phase_done")
              : failed ? t("component.video_card.phase_failed")
                : line ? t(`component.video_card.phase_${phaseOf(line.latest)}`)
                  : t("component.run_production.planned")}
              {unit.duration_s ? <span className="mono"> · {unit.duration_s}s</span> : null}</span>
            {config.lanes.map((lane) => unit.lanes[lane.key]
              ? <span key={lane.key} className="run-production__lane" title={`${lane.label}: ${unit.lanes[lane.key]}`}>{unit.lanes[lane.key]}</span>
              : null)}
          </button>;
        })}
      </div>
      {picked && <div className="run-production__unit-detail">
        <div className="workspace-app-flow__files-heading">
          <h4>{config.unit.name} {units.indexOf(picked) + 1}</h4>
          {picked.title && <span>{picked.title}</span>}
        </div>
        {config.lanes.map((lane) => picked.lanes[lane.key]
          ? <p key={lane.key}><small>{lane.label}</small><br />{picked.lanes[lane.key]}</p> : null)}
        {picked.line
          ? <MediaJobCard key={picked.line.root.id} resultJson={opened.current.get(picked.line.root.id) || "{}"} />
          : <p className="run-production__hint">{t("component.run_production.not_started")}</p>}
      </div>}
    </>}
  </section>;
}
