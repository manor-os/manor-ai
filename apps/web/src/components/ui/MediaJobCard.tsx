/**
 * MediaJobCard — one generated video clip, from the moment it is requested.
 *
 * While generating it shows which phase the job is in (queued, submitting,
 * generating, saving), the time spent and roughly how long is left. When done
 * it plays the clip, lists every version (v1, v2, …) and lets the person
 * adjust the settings and regenerate, or ask for another take. Earlier
 * versions are kept. Updates arrive over the socket (`manor:media-job-update`,
 * `manor:video-ready`) with a slow poll as the fallback.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, resolveDisplayMediaUrl, type MediaJob, type MediaJobPhase } from "../../lib/api";
import { getAuthToken, isSameAuthIdentity } from "../../lib/authToken";
import { displayReadInterval } from "../../lib/readRefreshPolicy";
import { usePageVisible } from "../../lib/usePageVisible";
import { t } from "../../lib/i18n";
import Button from "./Button";
import TabSwitcher from "./TabSwitcher";
import Textarea from "./Textarea";

const STEPS: MediaJobPhase[] = ["queued", "submitting", "generating", "saving"];
const DURATIONS = ["5", "10"];
const RESOLUTIONS = ["720p", "1080p"];
const ASPECTS = ["16:9", "9:16", "1:1"];
const POLL_MS = 20000;
const UNKNOWN_LINEAGE_RECONCILE_MS = 60_000;

const isTerminal = (job: Partial<MediaJob> | null) => job?.status === "completed" || job?.status === "failed";

export function phaseOf(job: Partial<MediaJob>): MediaJobPhase {
  if (job.status === "completed") return "done";
  if (job.status === "failed") return "failed";
  return job.progress?.phase || (job.status === "processing" ? "submitting" : "queued");
}

function parseResult(resultJson: string): Partial<MediaJob> | null {
  try {
    const data = JSON.parse(resultJson);
    if (!data || typeof data !== "object") return null;
    // Tool results name the job `job_id`; the jobs API names it `id`.
    return { ...data, id: data.id || data.job_id };
  } catch {
    return null;
  }
}

function clock(seconds: number) {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function useElapsed(job: Partial<MediaJob>) {
  const since = job.started_at || job.created_at;
  const running = !isTerminal(job) && Boolean(since);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);
  return since ? (now - new Date(since).getTime()) / 1000 : null;
}

/** Seconds left, counted down from the server's estimate between updates. */
function useRemaining(eta: number | null | undefined) {
  const [deadline, setDeadline] = useState<number | null>(null);
  useEffect(() => {
    setDeadline(eta != null ? Date.now() + eta * 1000 : null);
  }, [eta]);
  return deadline != null ? Math.max(0, (deadline - Date.now()) / 1000) : null;
}

export function remainingLabel(seconds: number): string {
  if (seconds > 60) return t("component.video_card.eta_minutes", { minutes: Math.ceil(seconds / 60) });
  if (seconds > 10) return t("component.video_card.eta_seconds", { n: Math.ceil(seconds / 5) * 5 });
  return t("component.video_card.eta_soon");
}

function ClipPlayer({ url }: { url: string }) {
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let cancelled = false;
    let revoke = () => {};
    setSrc(null);
    setFailed(false);
    resolveDisplayMediaUrl(url)
      .then((resolved) => {
        revoke = resolved.revoke;
        if (!cancelled) setSrc(resolved.url);
        else resolved.revoke();
      })
      .catch(() => { if (!cancelled) setFailed(true); });
    return () => { cancelled = true; revoke(); };
  }, [url]);
  const frame: React.CSSProperties = {
    width: "100%", maxWidth: 360, aspectRatio: "16 / 9", borderRadius: 10,
    display: "flex", alignItems: "center", justifyContent: "center",
  };
  if (src) {
    return <video src={src} controls playsInline preload="metadata" style={{ ...frame, aspectRatio: "auto", background: "#000", display: "block" }} />;
  }
  return (
    <div style={{ ...frame, background: "var(--surface-sunken)" }}>
      <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
        {failed ? t("component.video_card.preview_load_failed") : t("component.video_card.loading_preview")}
      </span>
    </div>
  );
}

function Meta({ job }: { job: Partial<MediaJob> }) {
  const params = job.params || {};
  const parts = [
    job.model,
    (job.duration_seconds || params.duration) ? t("component.video_card.seconds", { n: job.duration_seconds || params.duration }) : null,
    params.resolution,
    params.aspect_ratio,
    job.credits ? `${job.credits} ${t("component.video_card.credits")}` : null,
  ].filter(Boolean);
  return <div className="mono" style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 6 }}>{parts.join(" · ")}</div>;
}

function Progress({ job }: { job: Partial<MediaJob> }) {
  const phase = phaseOf(job);
  const current = Math.max(0, STEPS.indexOf(phase));
  const elapsed = useElapsed(job);
  const remaining = useRemaining(job.eta_seconds);
  // The progress fills a frame the size of the coming clip, so the page does
  // not jump when a clip starts regenerating or finishes.
  const [w, h] = String(job.params?.aspect_ratio || "16:9").split(":").map(Number);
  return (
    <div role="status" aria-live="polite" style={{
      width: "100%", maxWidth: 360, aspectRatio: w > 0 && h > 0 ? `${w} / ${h}` : "16 / 9", boxSizing: "border-box",
      display: "flex", flexDirection: "column", justifyContent: "flex-end", padding: 12, borderRadius: 10, background: "var(--surface-sunken)",
    }}>
      <div style={{ display: "flex", gap: 4 }}>
        {STEPS.map((step, index) => (
          <div
            key={step}
            style={{
              flex: 1, height: 3, borderRadius: 2,
              background: index < current ? "var(--ink)" : index === current ? "var(--text-faint)" : "var(--border-default)",
            }}
          />
        ))}
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, marginTop: 8, fontSize: 12 }}>
        <span style={{ color: "var(--text-strong)", fontWeight: 500 }}>{t(`component.video_card.phase_${phase}`)}</span>
        <span className="mono" style={{ color: "var(--text-muted)", fontSize: 11 }}>
          {elapsed != null ? clock(elapsed) : ""}
          {remaining != null ? ` · ${remainingLabel(remaining)}` : ""}
        </span>
      </div>
      <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 4 }}>
        {remaining == null ? t("component.video_card.usually_takes") : t("component.video_card.leave_hint")}
      </div>
    </div>
  );
}

type Draft = { prompt: string; duration: string; resolution: string; aspect: string };

function draftFrom(job: Partial<MediaJob>): Draft {
  const params = job.params || {};
  return {
    prompt: String(params.source_prompt || job.prompt || ""),
    duration: String(job.duration_seconds || params.duration || DURATIONS[0]),
    resolution: String(params.resolution || "720p"),
    aspect: String(params.aspect_ratio || "16:9"),
  };
}

/** The usual choices plus the clip's own value, so an unlisted one (480p, 4:3) is kept, not swapped. */
function choicesWith(options: string[], current: string): string[] {
  return options.includes(current) ? options : [current, ...options];
}

export default function MediaJobCard({ resultJson }: { resultJson: string }) {
  const initial = useMemo(() => parseResult(resultJson), [resultJson]);
  const [versions, setVersions] = useState<Partial<MediaJob>[]>(() => (initial ? [initial] : []));
  const [selectedId, setSelectedId] = useState(initial?.id);
  const selectedIdRef = useRef(selectedId);
  selectedIdRef.current = selectedId;
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lineageRoot, setLineageRoot] = useState<string | null>(null);
  const visible = usePageVisible();
  const queryClient = useQueryClient();
  const lineageEventTimer = useRef<number | null>(null);
  const lineageRefreshQueued = useRef(false);
  const lineageRefreshDueAt = useRef(0);
  const lineageRefreshIdentity = useRef<ReturnType<typeof getAuthToken>>(null);
  const unknownLineageStale = useRef(false);
  const lineageRefreshBounded = useRef(false);
  const versionsRef = useRef(versions);

  useEffect(() => { versionsRef.current = versions; }, [versions]);

  useEffect(() => {
    setVersions(initial ? [initial] : []);
    setSelectedId(initial?.id);
    setLineageRoot(null);
  }, [initial]);

  const selected = Math.max(0, versions.findIndex(item => item.id === selectedId));
  const job = versions[selected] || null;
  const latest = versions[versions.length - 1] || null;
  const jobId = initial?.id;
  // A job that failed can still be edited and tried again; a call refused
  // before any job existed has no id.
  const canRegenerate = Boolean(jobId) && (initial?.kind || "video") === "video";

  const merge = useCallback((update: Partial<MediaJob> & { job_id?: string }) => {
    const id = update.id || update.job_id;
    if (!id) return;
    setVersions((current) => current.map((item) => (item.id === id ? { ...item, ...update, id } : item)));
  }, []);

  const unknownLineageDelay = useCallback((): number | false => {
    const state = queryClient.getQueryState(["media-job-versions", lineageRoot]);
    const interval = displayReadInterval(state || {}, UNKNOWN_LINEAGE_RECONCILE_MS);
    if (interval === false) return false;
    const lastAttemptAt = Math.max(state?.dataUpdatedAt || 0, state?.errorUpdatedAt || 0);
    return Math.max(0, interval - Math.max(0, Date.now() - lastAttemptAt));
  }, [lineageRoot, queryClient]);

  const scheduleLineageRefresh = useCallback((delay: number | false, bounded = false) => {
    if (delay === false) return;
    if (!lineageRoot) return;
    if (!visible) {
      unknownLineageStale.current = true;
      return;
    }
    const dueAt = Date.now() + delay;
    if (lineageEventTimer.current !== null && lineageRefreshDueAt.current <= dueAt) return;
    if (lineageEventTimer.current !== null) window.clearTimeout(lineageEventTimer.current);
    lineageRefreshQueued.current = true;
    lineageRefreshBounded.current = bounded;
    lineageRefreshDueAt.current = dueAt;
    lineageRefreshIdentity.current = getAuthToken();
    lineageEventTimer.current = window.setTimeout(() => {
      lineageEventTimer.current = null;
      lineageRefreshDueAt.current = 0;
      if (!lineageRefreshQueued.current || !isSameAuthIdentity(lineageRefreshIdentity.current, getAuthToken())) return;
      if (lineageRefreshBounded.current) {
        // A shared observer may have refreshed (or failed) while this timer
        // waited. Re-evaluate before reading so errors cannot bypass the bound.
        const remaining = unknownLineageDelay();
        if (remaining === false) return;
        if (remaining > 0) {
          scheduleLineageRefresh(Math.max(500, remaining), true);
          return;
        }
      }
      lineageRefreshQueued.current = false;
      lineageRefreshBounded.current = false;
      void queryClient.invalidateQueries(
        { queryKey: ["media-job-versions", lineageRoot], exact: true },
        { cancelRefetch: false },
      );
    }, delay);
  }, [lineageRoot, queryClient, unknownLineageDelay, visible]);

  useEffect(() => {
    if (!visible && lineageEventTimer.current !== null) {
      window.clearTimeout(lineageEventTimer.current);
      lineageEventTimer.current = null;
      lineageRefreshDueAt.current = 0;
      unknownLineageStale.current = true;
    }
    if (visible && unknownLineageStale.current) {
      unknownLineageStale.current = false;
      const delay = unknownLineageDelay();
      scheduleLineageRefresh(delay === false ? false : Math.max(500, delay), true);
    }
  }, [lineageRoot, queryClient, scheduleLineageRefresh, visible]);

  useEffect(() => {
    const onEvent = (event: Event) => {
      const update = (event as CustomEvent<Partial<MediaJob>>).detail || {};
      merge(update);
      const id = update.id || (update as { job_id?: string }).job_id;
      if (update.lineage_root === lineageRoot || (id && versionsRef.current.some((item) => item.id === id && update.lineage_root))) {
        scheduleLineageRefresh(500);
      } else if (!update.lineage_root) {
        // A real media progress event may omit lineage metadata. At most one
        // refresh per lineage every 60s turns a burst into bounded recovery.
        const delay = unknownLineageDelay();
        scheduleLineageRefresh(delay === false ? false : Math.max(500, delay), true);
      }
    };
    window.addEventListener("manor:media-job-update", onEvent);
    window.addEventListener("manor:video-ready", onEvent);
    return () => {
      window.removeEventListener("manor:media-job-update", onEvent);
      window.removeEventListener("manor:video-ready", onEvent);
      if (lineageEventTimer.current !== null) window.clearTimeout(lineageEventTimer.current);
      lineageEventTimer.current = null;
    };
  }, [lineageRoot, merge, queryClient, scheduleLineageRefresh, unknownLineageDelay]);

  const pendingId = latest && !isTerminal(latest) ? latest.id : undefined;
  const queryJobId = pendingId || jobId;
  const mediaJobQuery = useQuery({
    queryKey: ["media-job", queryJobId],
    queryFn: ({ signal }) => api.mediaJobs.get(queryJobId!, signal),
    enabled: visible && Boolean(queryJobId),
    staleTime: 0,
    retry: false,
    refetchInterval: (query) => displayReadInterval(query.state, pendingId ? POLL_MS : false),
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
  });

  useEffect(() => {
    const full = mediaJobQuery.data;
    if (!full) return;
    merge(full);
    if (full.id === jobId) setLineageRoot(full.lineage_root || full.id);
  }, [jobId, mediaJobQuery.data, merge]);

  const versionsQuery = useQuery({
    queryKey: ["media-job-versions", lineageRoot],
    queryFn: ({ signal }) => api.mediaJobs.versions(lineageRoot!, AbortSignal.any([signal, AbortSignal.timeout(15_000)])),
    enabled: visible && canRegenerate && Boolean(lineageRoot),
    staleTime: 0,
    retry: false,
    refetchInterval: (query) => displayReadInterval(query.state,
      pendingId ? POLL_MS : versions.length > 5 ? 300_000 : 120_000),
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
  });

  // Apply only a new lineage read: picking a version must not replay an older
  // cached list over progress that socket events merged since.
  useEffect(() => {
    const all = versionsQuery.data;
    if (!all?.length) return;
    const selected = selectedIdRef.current;
    setVersions((current) => {
      if (!selected || all.some((item) => item.id === selected)) return all;
      const selectedVersion = current.find((item) => item.id === selected);
      return selectedVersion ? [...all, selectedVersion] : all;
    });
  }, [versionsQuery.data]);

  const regenerate = async (input: Parameters<typeof api.mediaJobs.regenerate>[1]) => {
    if (!job?.id) return;
    const identity = getAuthToken();
    setBusy(true);
    setError(null);
    try {
      const next = await api.mediaJobs.regenerate(job.id, input);
      if (!isSameAuthIdentity(identity, getAuthToken())) return;
      // An older lineage read must not erase the newly created version. Publish
      // the receipt to the shared cache so every card sees the same history.
      const queryKey = ["media-job-versions", next.lineage_root || lineageRoot || jobId];
      await queryClient.cancelQueries({ queryKey, exact: true });
      if (!isSameAuthIdentity(identity, getAuthToken())) return;
      queryClient.setQueryData<Partial<MediaJob>[]>(queryKey, current => [
        ...(current || versions).filter(item => item.id !== next.id), next,
      ]);
      setVersions((current) => [...current.filter(item => item.id !== next.id), next]);
      setSelectedId(next.id);
      setDraft(null);
    } catch (err) {
      if (!isSameAuthIdentity(identity, getAuthToken())) return;
      setError(t("component.video_card.regenerate_failed", { error: err instanceof Error ? err.message : String(err) }));
    } finally {
      setBusy(false);
    }
  };

  if (!job) return null;
  const url = job.result_url || (job as Record<string, any>).video_url || (job as Record<string, any>).url || "";
  const failed = job.status === "failed" || Boolean(job.error);
  const regenerating = Boolean(pendingId);

  return (
    <div style={{ padding: "6px 8px", maxWidth: 380 }}>
      {url ? <ClipPlayer url={url} /> : failed ? (
        <div style={{ padding: "8px 10px", borderRadius: 8, background: "var(--chip-danger-bg)", maxWidth: 360 }}>
          <div style={{ fontSize: 11, color: "var(--chip-danger-fg)", fontWeight: 600 }}>{t("component.video_card.generation_failed")}</div>
          <div style={{ fontSize: 11, color: "var(--text-default)", marginTop: 2 }}>{job.error || t("component.video_card.unknown_error")}</div>
        </div>
      ) : <Progress job={job} />}
      <Meta job={job} />

      {versions.length > 1 && (
        <div role="group" aria-label={t("component.video_card.versions")} style={{ display: "flex", gap: 4, flexWrap: "wrap", marginTop: 8 }}>
          {versions.map((item, index) => (
            <button
              key={item.id || index}
              type="button"
              onClick={() => setSelectedId(item.id)}
              aria-pressed={index === selected}
              className="mono"
              style={{
                fontSize: 11, padding: "2px 8px", borderRadius: 6, border: "none", cursor: "pointer",
                background: index === selected ? "var(--ink)" : "rgba(29, 29, 31,0.045)",
                color: index === selected ? "var(--surface-panel)" : "var(--text-muted)",
              }}
            >
              {t("component.video_card.version", { n: index + 1 })}
              {!isTerminal(item) ? " …" : ""}
            </button>
          ))}
        </div>
      )}

      {/* Kept, disabled, while a version is being made so the card keeps its size. */}
      {canRegenerate && (isTerminal(job) || regenerating) && !draft && (
        <div style={{ display: "flex", gap: 6, marginTop: 10, flexWrap: "wrap" }}>
          <Button size="sm" variant="outline" disabled={regenerating} onClick={() => setDraft(draftFrom(job))}>
            {failed ? t("component.video_card.edit_and_retry") : t("component.video_card.adjust")}
          </Button>
          {!failed && (
            <Button size="sm" variant="ghost" loading={busy} disabled={regenerating} onClick={() => void regenerate({ keep_seed: false })}
              title={t("component.video_card.another_take_hint")}>
              {t("component.video_card.another_take")}
            </Button>
          )}
        </div>
      )}

      {draft && (
        <div style={{ marginTop: 10, display: "grid", gap: 8 }}>
          <Textarea
            value={draft.prompt}
            rows={3}
            ariaLabel={t("component.video_card.prompt_label")}
            onChange={(event) => setDraft({ ...draft, prompt: event.target.value })}
          />
          {([
            ["duration", DURATIONS, (value: string) => t("component.video_card.seconds", { n: value })],
            ["resolution", RESOLUTIONS, (value: string) => value],
            ["aspect", ASPECTS, (value: string) => value],
          ] as const).map(([field, options, label]) => (
            <div key={field} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
              <span style={{ fontSize: 11, color: "var(--text-muted)" }}>{t(`component.video_card.${field}`)}</span>
              <TabSwitcher
                size="sm"
                ariaLabel={t(`component.video_card.${field}`)}
                tabs={choicesWith(options, draft[field]).map((value) => ({ key: value, label: label(value) }))}
                value={draft[field]}
                onChange={(value) => setDraft({ ...draft, [field]: value })}
              />
            </div>
          ))}
          <div style={{ fontSize: 11, color: "var(--text-faint)" }}>
            {t("component.video_card.new_version_note", { next: versions.length + 1, current: selected + 1 })}
          </div>
          <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
            <Button size="sm" variant="ghost" onClick={() => { setDraft(null); setError(null); }}>{t("component.video_card.cancel")}</Button>
            <Button
              size="sm"
              loading={busy}
              disabled={!draft.prompt.trim()}
              onClick={() => void regenerate({
                prompt: draft.prompt.trim(),
                duration: Number(draft.duration),
                resolution: draft.resolution,
                aspect_ratio: draft.aspect,
                keep_seed: true,
              })}
            >
              {t("component.video_card.regenerate")}
            </Button>
          </div>
        </div>
      )}
      {error && <div role="alert" style={{ fontSize: 11, color: "var(--chip-danger-fg)", marginTop: 6 }}>{error}</div>}
    </div>
  );
}
