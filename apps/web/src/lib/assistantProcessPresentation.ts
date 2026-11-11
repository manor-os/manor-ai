import { useEffect, useRef, useState } from "react";
import { nextTypewriterSlice, TYPEWRITER_TICK_MS } from "./chatTypewriter";
import type { AssistantBlock, AssistantProcessBlock } from "./chatStream";

export const LIVE_PROCESS_CLOCK_TICK_MS = 1000;
export const LIVE_CLOCK_RESYNC_MS = 500;

export function normalizeDurationMs(ms?: number | null): number {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms <= 0) return 0;
  return ms;
}

export function formatProcessDuration(ms?: number | null): string {
  const normalized = normalizeDurationMs(ms);
  if (normalized < 1000) return "";
  const totalSeconds = Math.round(normalized / 1000);
  if (totalSeconds <= 0) return "";
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (!minutes) return `${seconds}s`;
  return `${minutes}m ${seconds}s`;
}

export function shouldResyncLiveClock(
  localElapsedMs: number,
  serverDurationMs: number,
  thresholdMs = LIVE_CLOCK_RESYNC_MS,
): boolean {
  return serverDurationMs > localElapsedMs + thresholdMs;
}

export function resolveLiveProcessClockMs({
  live,
  serverDurationMs,
  localElapsedMs,
  startedAtMs,
  endedAtMs,
  nowMs = Date.now(),
}: {
  live: boolean;
  serverDurationMs: number;
  localElapsedMs: number;
  startedAtMs?: number;
  endedAtMs?: number;
  nowMs?: number;
}): number {
  if (normalizeDurationMs(startedAtMs)) {
    if (normalizeDurationMs(endedAtMs)) return Math.max(0, endedAtMs! - startedAtMs!);
    if (live) return Math.max(serverDurationMs, nowMs - startedAtMs!, 0);
  }
  if (!live) return serverDurationMs;
  if (shouldResyncLiveClock(localElapsedMs, serverDurationMs)) return serverDurationMs;
  return Math.max(localElapsedMs, 0);
}

export function processClockTiming(block: AssistantProcessBlock) {
  const starts = block.steps.map((step) => normalizeDurationMs(step.started_at_ms)).filter(Boolean);
  const ends = block.steps.map((step) => normalizeDurationMs(step.ended_at_ms)).filter(Boolean);
  const startedAtMs = normalizeDurationMs(block.started_at_ms) || (starts.length ? Math.min(...starts) : undefined);
  const endedAtMs = normalizeDurationMs(block.ended_at_ms) || undefined;
  // Legacy messages have step timestamps but no process clock. In particular,
  // a running nested tool must not reset the clock when duration_ms is absent.
  const serverDurationMs = normalizeDurationMs(block.duration_ms) || (
    startedAtMs && ends.length ? Math.max(0, Math.max(...ends) - startedAtMs) : 0
  );
  return { startedAtMs, endedAtMs, serverDurationMs };
}

/** How long one finished process took (its own clock, else its steps). */
function processDurationMs(block: AssistantProcessBlock): number {
  const { startedAtMs, endedAtMs, serverDurationMs } = processClockTiming(block);
  return startedAtMs && endedAtMs ? Math.max(0, endedAtMs - startedAtMs) : serverDurationMs;
}

/**
 * One reply drawn across messages (the answer that continues past a decision
 * card) has one process line, at the top: the first part's process takes the
 * steps of every part and their summed time, and the parts below lose theirs.
 * Returns the blocks for each part, in order.
 */
export function joinReplyProcesses(parts: (AssistantBlock[] | null | undefined)[]): (AssistantBlock[] | undefined)[] {
  const blocks = parts.map((part) => (Array.isArray(part) ? part : []));
  const processes = blocks.flatMap((part) =>
    part.filter((block): block is AssistantProcessBlock => block.type === "process"));
  if (processes.length < 2) return parts.map((part) => part ?? undefined);
  const last = processes[processes.length - 1]!;
  const joined: AssistantProcessBlock = {
    ...processes[0]!,
    steps: processes.flatMap((process) => process.steps || []),
    duration_ms: processes.reduce((total, process) => total + processDurationMs(process), 0),
    started_at_ms: undefined,
    ended_at_ms: undefined,
    lifecycle_status: last.lifecycle_status,
    outcome_status: last.outcome_status,
    status: last.status,
  };
  const firstWithProcess = blocks.findIndex((part) => part.some((block) => block.type === "process"));
  return blocks.map((part, index) => {
    if (index === firstWithProcess) {
      return part.map((block) => (block.type === "process" ? joined : block));
    }
    return part.filter((block) => block.type !== "process");
  });
}

export function isLiveAssistantProcess(
  block: AssistantProcessBlock,
  streaming: boolean,
  waitingForHuman: boolean,
): boolean {
  return streaming && !waitingForHuman && !normalizeDurationMs(block.ended_at_ms)
    && (!block.lifecycle_status || block.lifecycle_status === "running");
}

export function nextCollapsedThinkingFrame(shown: string, target: string): string {
  if (!target) return "";
  if (shown === target) return shown;
  if (target.startsWith(shown)) {
    const [chunk] = nextTypewriterSlice(target.slice(shown.length));
    return shown + chunk;
  }
  const [chunk] = nextTypewriterSlice(target);
  return chunk;
}

export function useLiveProcessClock({
  live,
  serverDurationMs,
  startedAtMs,
  endedAtMs,
}: {
  live: boolean;
  serverDurationMs?: number | null;
  startedAtMs?: number;
  endedAtMs?: number;
}): number {
  const serverMs = normalizeDurationMs(serverDurationMs);
  const [localElapsedMs, setLocalElapsedMs] = useState(serverMs);
  const originRef = useRef<{ elapsedMs: number; wallMs: number } | null>(null);

  useEffect(() => {
    if (!live) {
      originRef.current = null;
      setLocalElapsedMs(serverMs);
      return;
    }

    const now = Date.now();
    const localElapsed = originRef.current
      ? originRef.current.elapsedMs + Math.max(0, now - originRef.current.wallMs)
      : serverMs;
    if (!originRef.current || shouldResyncLiveClock(localElapsed, serverMs)) {
      originRef.current = { elapsedMs: serverMs, wallMs: now };
    }
    setLocalElapsedMs(
      originRef.current.elapsedMs + Math.max(0, now - originRef.current.wallMs),
    );

    const id = setInterval(() => {
      const origin = originRef.current;
      if (!origin) return;
      setLocalElapsedMs(origin.elapsedMs + Math.max(0, Date.now() - origin.wallMs));
    }, LIVE_PROCESS_CLOCK_TICK_MS);

    return () => clearInterval(id);
  }, [live, serverMs]);

  return resolveLiveProcessClockMs({
    live,
    serverDurationMs: serverMs,
    localElapsedMs,
    startedAtMs,
    endedAtMs,
  });
}

export function useTypedCollapsedThinking(target: string, enabled: boolean): string {
  const [shown, setShown] = useState("");

  useEffect(() => {
    if (!enabled || !target) {
      setShown("");
      return;
    }

    setShown((prev) => nextCollapsedThinkingFrame(target.startsWith(prev) ? prev : "", target));

    const id = setInterval(() => {
      setShown((prev) => nextCollapsedThinkingFrame(prev, target));
    }, TYPEWRITER_TICK_MS);

    return () => clearInterval(id);
  }, [enabled, target]);

  return enabled ? shown : "";
}
