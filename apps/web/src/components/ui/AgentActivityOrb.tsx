import {
  ThinkingOrb,
  type OrbSize,
  type OrbState,
} from "thinking-orbs";
import type {
  AssistantBlock,
  AssistantProcessStep,
  ChatMessage,
  SubAgentEvent,
  ToolCall,
} from "../../lib/chatStream";
import { getLocale } from "../../lib/i18n";

export type AgentActivity = OrbState;

const ACTIVITY_LABELS: Record<"en" | "es" | "zh", Record<AgentActivity, string>> = {
  en: {
    working: "Working",
    searching: "Searching",
    solving: "Thinking",
    listening: "Listening",
    connecting: "Connecting",
    weaving: "Coordinating agents",
    composing: "Composing response",
    breathing: "Preparing",
    shaping: "Creating",
  },
  es: {
    working: "Trabajando",
    searching: "Buscando",
    solving: "Pensando",
    listening: "Escuchando",
    connecting: "Conectando",
    weaving: "Coordinando agentes",
    composing: "Redactando respuesta",
    breathing: "Preparando",
    shaping: "Creando",
  },
  zh: {
    working: "正在执行",
    searching: "正在搜索",
    solving: "正在思考",
    listening: "正在聆听",
    connecting: "正在连接",
    weaving: "正在协调 Agent",
    composing: "正在组织回答",
    breathing: "正在准备",
    shaping: "正在生成",
  },
};

const SEARCH_PATTERN = /(?:^|[_:.\-/])(search|find|query|lookup|browse|research|crawl|web)(?:$|[_:.\-/])/i;
const CONNECT_PATTERN = /(?:^|[_:.\-/])(connect|oauth|login|auth|integration|gateway|handshake)(?:$|[_:.\-/])/i;
const SHAPE_PATTERN = /(?:^|[_:.\-/])(create|generate|render|export|write|edit|patch|apply|build|image|video|audio|slide|presentation|document|spreadsheet|diagram|canvas)(?:$|[_:.\-/])/i;
const SOLVE_PATTERN = /(?:^|[_:.\-/])(plan|reason|solve|analyze|analyse|invoke_skill|manor|code)(?:$|[_:.\-/])/i;
const INSPECT_PATTERN = /(?:^|[_:.\-/])(read|list|scan|retrieve|fetch|inspect|parse|load|open)(?:$|[_:.\-/])/i;

function currentLocale(): "en" | "es" | "zh" {
  const locale = getLocale().toLowerCase();
  if (locale.startsWith("zh")) return "zh";
  if (locale.startsWith("es")) return "es";
  return "en";
}

export function agentActivityLabel(activity: AgentActivity): string {
  return ACTIVITY_LABELS[currentLocale()][activity];
}

function isPendingStatus(status: unknown): boolean {
  const normalized = String(status || "pending").toLowerCase();
  return normalized === "pending" || normalized === "running";
}

function activityForOperation(name: string): AgentActivity {
  const normalized = name.trim().toLowerCase();
  if (SEARCH_PATTERN.test(normalized)) return "searching";
  if (SHAPE_PATTERN.test(normalized)) return "shaping";
  if (CONNECT_PATTERN.test(normalized) || normalized.startsWith("mcp__")) return "connecting";
  if (SOLVE_PATTERN.test(normalized)) return "solving";
  if (INSPECT_PATTERN.test(normalized)) return "working";
  return "working";
}

function runningSubAgents(events: SubAgentEvent[] | undefined): SubAgentEvent[] {
  return (events || []).filter((event) => isPendingStatus(event.status));
}

function activeToolName(tools: ToolCall[] | undefined): string {
  const active = [...(tools || [])]
    .reverse()
    .find((tool) => isPendingStatus(tool.status || (tool.result ? "success" : "pending")));
  return active?.activeChild || active?.name || "";
}

function activeProcessStep(blocks: AssistantBlock[] | undefined): AssistantProcessStep | undefined {
  const steps = (blocks || [])
    .filter((block) => block.type === "process")
    .flatMap((block) => block.steps || []);
  return [...steps].reverse().find((step) => isPendingStatus(step.status));
}

/** Map the real streaming envelope to one of thinking-orbs' visual states. */
export function inferAgentActivity(message?: Pick<ChatMessage, "content" | "tool_calls" | "assistant_blocks" | "sub_agent_events"> | null): AgentActivity {
  if (!message) return "breathing";
  if (runningSubAgents(message.sub_agent_events).length > 0) return "weaving";

  const toolName = activeToolName(message.tool_calls);
  if (toolName) return activityForOperation(toolName);
  if (message.content?.trim()) return "composing";

  const processStep = activeProcessStep(message.assistant_blocks);
  if (processStep?.name) return activityForOperation(processStep.name);
  return "solving";
}

export function activityForRuntimeStage(stageLabel: string): AgentActivity {
  return activityForOperation(stageLabel || "working");
}

export default function AgentActivityOrb({
  activity,
  label,
  size = 20,
  displaySize,
  iconOnly = false,
  paused = false,
  className = "",
}: {
  activity: AgentActivity;
  label?: string;
  size?: OrbSize;
  displaySize?: number;
  iconOnly?: boolean;
  paused?: boolean;
  className?: string;
}) {
  const resolvedLabel = label || agentActivityLabel(activity);
  const orb = (
    <ThinkingOrb
      state={activity}
      size={size}
      theme="light"
      paused={paused}
      className="agent-activity-orb__canvas"
      aria-label={iconOnly ? resolvedLabel : undefined}
      aria-hidden={iconOnly ? undefined : true}
      style={displaySize ? { width: displaySize, height: displaySize } : undefined}
    />
  );

  if (iconOnly) {
    return (
      <span className={`agent-activity-orb agent-activity-orb--icon ${className}`.trim()} title={resolvedLabel}>
        {orb}
      </span>
    );
  }

  return (
    <span
      className={`agent-activity-orb agent-activity-orb--status ${className}`.trim()}
      role="status"
      aria-live="polite"
      aria-label={resolvedLabel}
    >
      {orb}
      <span className="agent-activity-orb__label">{resolvedLabel}</span>
    </span>
  );
}
