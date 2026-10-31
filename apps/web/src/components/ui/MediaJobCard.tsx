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
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, resolveDisplayMediaUrl, type MediaJob, type MediaJobPhase } from "../../lib/api";
import { t } from "../../lib/i18n";
import Button from "./Button";
import TabSwitcher from "./TabSwitcher";
import Textarea from "./Textarea";

const STEPS: MediaJobPhase[] = ["queued", "submitting", "generating", "saving"];
const DURATIONS = ["5", "10"];
const RESOLUTIONS = ["720p", "1080p"];
const ASPECTS = ["16:9", "9:16", "1:1"];
const POLL_MS = 20000;

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
  const [selected, setSelected] = useState(0);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setVersions(initial ? [initial] : []);
    setSelected(0);
  }, [initial]);

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

  // Load the full record (progress, ETA) and every version of this clip.
  useEffect(() => {
    if (!jobId || !canRegenerate) return;
    let active = true;
    api.mediaJobs.get(jobId)
      .then((full) => api.mediaJobs.versions(full.lineage_root || full.id).catch(() => [full]))
      .then((all) => {
        if (!active || !all.length) return;
        setVersions(all);
        setSelected(Math.max(0, all.findIndex((item) => item.id === jobId)));
      })
      .catch(() => { /* the tool result still renders */ });
    return () => { active = false; };
  }, [jobId, canRegenerate]);

  useEffect(() => {
    const onEvent = (event: Event) => merge((event as CustomEvent).detail || {});
    window.addEventListener("manor:media-job-update", onEvent);
    window.addEventListener("manor:video-ready", onEvent);
    return () => {
      window.removeEventListener("manor:media-job-update", onEvent);
      window.removeEventListener("manor:video-ready", onEvent);
    };
  }, [merge]);

  const pendingId = latest && !isTerminal(latest) ? latest.id : undefined;
  useEffect(() => {
    if (!pendingId) return;
    const timer = window.setInterval(() => {
      api.mediaJobs.get(pendingId).then(merge).catch(() => {});
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [pendingId, merge]);

  const regenerate = async (input: Parameters<typeof api.mediaJobs.regenerate>[1]) => {
    if (!job?.id) return;
    setBusy(true);
    setError(null);
    try {
      const next = await api.mediaJobs.regenerate(job.id, input);
      setVersions((current) => [...current, next]);
      setSelected(versions.length);
      setDraft(null);
    } catch (err) {
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
              onClick={() => setSelected(index)}
              aria-pressed={index === selected}
              className="mono"
              style={{
                fontSize: 11, padding: "2px 8px", borderRadius: 6, border: "none", cursor: "pointer",
                background: index === selected ? "var(--ink)" : "rgba(28,25,23,0.045)",
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
