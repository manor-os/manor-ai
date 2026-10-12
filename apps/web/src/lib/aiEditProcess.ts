import type { AssistantBlock, AssistantProcessStep, ChatMessage } from "./chatStream";
import { t } from "./i18n";

const EDIT_STEPS = ["read_current_file", "generate_patch", "apply_patch", "verify_patch"] as const;

/** Freeze unfinished local clocks at the real editor terminal boundary. */
export function finishAiEditProgress(messages: ChatMessage[], timestamp: string): ChatMessage[] {
  const last = messages.at(-1);
  if (last?.role !== "assistant" || last.timestamp !== timestamp) return messages;
  const endedAt = Date.now();
  return [...messages.slice(0, -1), {
    ...last,
    tool_calls: last.tool_calls?.map((tool) => tool.name.startsWith("ai_edit_") && (tool.status === "pending" || (tool.status === "error" && !tool.result))
      ? { ...tool, duration: typeof tool.startedAt === "number" ? `${Math.max(0, endedAt - tool.startedAt) / 1000}s` : undefined }
      : tool),
  }];
}

/** Project actual editor events into the same public process contract as Chat. */
export function projectAiEditAssistantBlocks(
  message: ChatMessage,
  streaming: boolean,
): AssistantBlock[] | undefined {
  const blocks = message.assistant_blocks;
  const tools = (message.tool_calls || []).filter((tool) =>
    EDIT_STEPS.some((step) => tool.name === `ai_edit_${step}`),
  );
  if (!tools.length) return blocks;
  const process = blocks?.find((block) => block.type === "process");
  const hostSteps = process?.type === "process" ? process.steps || [] : [];
  const lastSeq = Math.max(0, ...hostSteps.map((step, index) => step.seq || index + 1));
  const stopped = message.stop_reason === "cancelled" || message.meta?.stream_interrupted === true
    || process?.type === "process" && process.lifecycle_status === "cancelled";
  const waitingForHuman = process?.type === "process" && process.lifecycle_status === "waiting"
    || message.hitl_requests?.some((request) => !request.resolved);
  const failed = Boolean(message.stream_error || message.stop_reason === "error");
  const localSteps: AssistantProcessStep[] = tools.map((tool, index) => {
    const kind = tool.name.slice("ai_edit_".length);
    let file = "";
    let persistedReload = false;
    try {
      const args = JSON.parse(tool.arguments || "{}");
      file = String(args.file || "");
      persistedReload = args.stage === "persisted bytes reloaded";
    } catch { /* Public labels only. */ }
    const duration = tool.duration ? Number.parseFloat(tool.duration) * 1000 : undefined;
    const durationMs = Number.isFinite(duration) ? Math.max(0, duration!) : undefined;
    const pending = tool.status === "pending";
    const status = (stopped || waitingForHuman) && (pending || (tool.status === "error" && !tool.result)) ? "not_run"
      : pending && !streaming ? "error" : tool.status;
    const key = status === "not_run" ? waitingForHuman ? "waiting" : "stopped"
      : status === "error" ? "failed"
      : kind === "verify_patch" && tool.status === "success" ? persistedReload ? "saved" : "ready" : kind;
    return {
      id: `editor-${tool.name}`,
      seq: lastSeq + index + 1,
      kind: "tool",
      name: tool.name,
      display_name: t(`component.assistant_process.ai_edit.${kind}`, { file }),
      display_key: `component.assistant_process.ai_edit.${kind}`,
      display_params: { file },
      assistant_text: t(`component.assistant_process.ai_edit.explain_${key}`, { file }),
      status,
      started_at_ms: tool.startedAt,
      duration_ms: durationMs,
      ended_at_ms: typeof tool.startedAt === "number" && typeof durationMs === "number"
        ? tool.startedAt + durationMs : undefined,
    };
  });
  const localPending = streaming && tools.some((tool) => tool.status === "pending");
  const verify = tools.find((tool) => tool.name === "ai_edit_verify_patch");
  const lifecycle = stopped ? "cancelled" : waitingForHuman ? "waiting" : failed ? "failed"
    : process?.type === "process" && ["waiting", "failed", "cancelled"].includes(process.lifecycle_status || "")
      ? process.lifecycle_status
      : localPending ? "running" : verify?.status === "error" || (!streaming && verify?.status !== "success" && localSteps.some((step) => step.status === "error")) ? "failed"
        : streaming && verify?.status !== "success" ? "running" : "completed";
  const starts = [process?.type === "process" ? process.started_at_ms : undefined,
    ...localSteps.map((step) => step.started_at_ms)].filter((value): value is number => typeof value === "number");
  const startedAt = starts.length ? Math.min(...starts) : undefined;
  const ends = [process?.type === "process" ? process.ended_at_ms : undefined,
    ...localSteps.map((step) => step.ended_at_ms)].filter((value): value is number => typeof value === "number");
  const endedAt = ["completed", "failed", "cancelled"].includes(lifecycle || "") && ends.length
    ? Math.max(...ends) : undefined;
  const merged: AssistantBlock = {
    ...(process?.type === "process" ? process : { id: "editor-process", type: "process" as const }),
    lifecycle_status: lifecycle,
    status: lifecycle === "failed" ? "error" : lifecycle === "running" ? "running" : lifecycle === "waiting" ? "pending" : "completed",
    default_collapsed: lifecycle === "completed",
    started_at_ms: startedAt,
    ended_at_ms: endedAt,
    duration_ms: typeof startedAt === "number" && typeof endedAt === "number" ? Math.max(0, endedAt - startedAt)
      : process?.type === "process" ? process.duration_ms : undefined,
    steps: [...hostSteps, ...localSteps].sort((a, b) => (a.started_at_ms || 0) - (b.started_at_ms || 0)),
  };
  if (process) return blocks!.map((block) => block === process ? merged : block);
  const next = [...(blocks || [])];
  const finalIndex = next.findIndex((block) => block.type === "text" && block.phase === "final");
  next.splice(finalIndex < 0 ? 0 : finalIndex, 0, merged);
  if (!blocks?.length && message.content) next.push({ id: "editor-final", type: "text", phase: "final", text: String(message.content) });
  return next;
}
