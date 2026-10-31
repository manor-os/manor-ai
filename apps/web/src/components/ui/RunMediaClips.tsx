import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { api, type MediaJob } from "../../lib/api";
import { t } from "../../lib/i18n";
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
  const clips = useQuery({
    queryKey: ["media-jobs", "run", runId],
    queryFn: () => api.mediaJobs.forRun(runId),
    refetchInterval: running ? 5000 : false,
  });
  const { data, refetch } = clips;

  // A job this list has not seen yet may be a new clip of this run: look
  // again now rather than at the next poll. Known clips update themselves.
  useEffect(() => {
    const known = new Set((data || []).map((job) => job.id));
    const onEvent = (event: Event) => {
      const id = (event as CustomEvent).detail?.id;
      if (id && !known.has(id)) void refetch();
    };
    window.addEventListener("manor:media-job-update", onEvent);
    return () => window.removeEventListener("manor:media-job-update", onEvent);
  }, [data, refetch]);

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
