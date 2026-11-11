import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { api, type MediaJob } from "../../lib/api";
import { t } from "../../lib/i18n";
import { displayReadInterval } from "../../lib/readRefreshPolicy";
import { usePageVisible } from "../../lib/usePageVisible";
import MediaJobCard from "./MediaJobCard";

/**
 * One entry per clip, in the order the run made them: its first version (for
 * the clip's place and name) and its newest version (what the card opens on).
 */
export function clipLines(jobs: MediaJob[]): { root: MediaJob; latest: MediaJob }[] {
  const lines = new Map<string, { root: MediaJob; latest: MediaJob }>();
  for (const job of [...jobs].reverse()) {
    const key = job.lineage_root || job.id;
    const line = lines.get(key);
    if (!line) lines.set(key, { root: job, latest: job });
    else line.latest = job;
  }
  return [...lines.values()];
}

function clipLabel(job: MediaJob, index: number): string {
  const name = String(job.params?.output_name || "").split("/").pop() || "";
  return job.unit_id || name.replace(/\.[a-z0-9]+$/i, "") || t("component.run_media_clips.clip", { number: index + 1 });
}

/**
 * The video clips a Workflow run generated, each with its live progress,
 * versions and regenerate controls. Renders nothing for runs without clips.
 */
export default function RunMediaClips({ runId, running }: { runId: string; running: boolean }) {
  const visible = usePageVisible();
  const queryClient = useQueryClient();
  const eventTimer = useRef<number | null>(null);
  const pendingEvent = useRef(false);
  const lastUnknownReconcileAt = useRef(0);
  const clips = useQuery({
    queryKey: ["media-jobs", "run", runId],
    queryFn: ({ signal }) => api.mediaJobs.forRun(runId, AbortSignal.any([signal, AbortSignal.timeout(15_000)])),
    enabled: visible,
    retry: false,
    // A completed run can still receive a late persisted clip, but it does not
    // need the live rendering cadence.
    refetchInterval: (query) => displayReadInterval(query.state, running ? 15_000 : 60_000),
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
  });
  const { data } = clips;

  // New clips are coalesced. A payload without run_id cannot be attributed,
  // so it receives only a bounded reconciliation of the current run.
  useEffect(() => {
    const known = new Set((data || []).map((job) => job.id));
    const flush = () => {
      eventTimer.current = null;
      if (!pendingEvent.current) return;
      pendingEvent.current = false;
      void queryClient.invalidateQueries(
        { queryKey: ["media-jobs", "run", runId] },
        { cancelRefetch: false },
      );
    };
    const schedule = () => {
      if (eventTimer.current !== null) return;
      eventTimer.current = window.setTimeout(flush, 500);
    };
    const onEvent = (event: Event) => {
      const update = (event as CustomEvent<Partial<MediaJob>>).detail;
      if (!visible || !update) return;
      if (update.run_id === runId && update.id && !known.has(update.id)) {
        pendingEvent.current = true;
        schedule();
        return;
      }
      if (!update.run_id && Date.now() - lastUnknownReconcileAt.current >= 15_000) {
        lastUnknownReconcileAt.current = Date.now();
        pendingEvent.current = true;
        schedule();
      }
    };
    window.addEventListener("manor:media-job-update", onEvent);
    return () => {
      window.removeEventListener("manor:media-job-update", onEvent);
      if (eventTimer.current !== null) window.clearTimeout(eventTimer.current);
      eventTimer.current = null;
    };
  }, [data, queryClient, runId, visible]);

  // Each card follows its own clip once shown; handing it a fresh snapshot on
  // every poll would reset the version the person is looking at.
  const firstSeen = useRef(new Map<string, string>());
  const clipsOfRun = clipLines((data || []).filter((job) => job.kind === "video"));
  if (!clipsOfRun.length) return null;
  for (const { root, latest } of clipsOfRun) {
    if (!firstSeen.current.has(root.id)) firstSeen.current.set(root.id, JSON.stringify(latest));
  }
  // A clip counts as done once any of its versions finished.
  const finished = new Set((data || []).filter((job) => job.status === "completed").map((job) => job.lineage_root || job.id));
  const done = clipsOfRun.filter(({ root }) => finished.has(root.id)).length;
  return <section className="run-media-clips">
    <div className="workspace-app-flow__files-heading">
      <h4>{t("component.run_media_clips.title")}</h4>
      <span className="mono">{done} / {clipsOfRun.length}</span>
    </div>
    <ul className="run-media-clips__grid">{clipsOfRun.map(({ root }, index) => <li key={root.id}>
      <span className="run-media-clips__label mono">{clipLabel(root, index)}</span>
      <MediaJobCard resultJson={firstSeen.current.get(root.id) || "{}"} />
    </li>)}</ul>
  </section>;
}
