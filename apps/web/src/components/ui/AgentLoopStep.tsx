import { IconChevronDown } from "../icons";
import type { SubAgentEvent } from "../../lib/chatStream";
import { t } from "../../lib/i18n";
import {
  formatUserFacingLabel,
  formatUserFacingStructuredText,
} from "../../lib/taskDisplay";
import {
  parseMcpToolName,
  runtimeToolBadge,
} from "../../lib/toolRuntimeSurface";
import UserAvatar from "./UserAvatar";

function statusLabel(status?: string) {
  if (status === "completed") {
    return t("component.workspace_chat.agent_run_completed");
  }
  if (status === "blocked") {
    return t("component.workspace_chat.agent_run_blocked");
  }
  if (status === "failed") {
    return t("component.workspace_chat.agent_run_failed");
  }
  return t("component.workspace_chat.agent_run_running");
}

function toolLabel(name: string) {
  const parsed = parseMcpToolName(name);
  if (parsed) {
    return `${formatUserFacingLabel(parsed.serverKey)}: ${formatUserFacingLabel(parsed.actionKey)}`;
  }
  return formatUserFacingLabel(
    String(name || "tool").replace(/^mcp__/, "").replace(/__/g, " "),
  );
}

function compactText(value: unknown, maxLength = 120) {
  const text = formatUserFacingStructuredText(value)
    .replace(/\s+/g, " ")
    .trim();
  if (!text) return "";
  return text.length <= maxLength ? text : `${text.slice(0, maxLength - 1)}…`;
}

function toolDetail(value: unknown) {
  let args = value;
  if (typeof args === "string") {
    try {
      args = JSON.parse(args);
    } catch {
      return compactText(args);
    }
  }
  if (!args || typeof args !== "object" || Array.isArray(args)) return "";
  const record = args as Record<string, any>;
  const params =
    record.params && typeof record.params === "object" && !Array.isArray(record.params)
      ? record.params
      : {};
  for (const key of [
    "query",
    "path",
    "file_path",
    "filename",
    "url",
    "target",
    "goal",
    "action",
    "name",
  ]) {
    const valueForKey = record[key] ?? params[key];
    if (valueForKey !== undefined && valueForKey !== null && valueForKey !== "") {
      return compactText(valueForKey);
    }
  }
  return "";
}

function durationLabel(durationMs?: number) {
  if (
    typeof durationMs !== "number" ||
    !Number.isFinite(durationMs) ||
    durationMs <= 0
  ) {
    return "";
  }
  return durationMs < 1000
    ? `${Math.round(durationMs)}ms`
    : `${(durationMs / 1000).toFixed(durationMs < 10000 ? 1 : 0)}s`;
}

export default function AgentLoopStep({ run }: { run: SubAgentEvent }) {
  const status = run.status || "running";
  const tools = Array.isArray(run.tools) ? run.tools : [];
  const activeTool = [...tools]
    .reverse()
    .find((tool) => tool.status === "running");
  const isRunning = status === "running";
  const activity = activeTool
    ? t("component.workspace_chat.agent_run_using").replace(
        "{tool}",
        toolLabel(activeTool.name),
      )
    : run.objective ||
      (isRunning
        ? t("component.workspace_chat.agent_run_preparing")
        : t("component.workspace_chat.agent_run_actions")
            .replace(
              "{count}",
              String(tools.length || run.tool_calls_made?.length || 0),
            )
            .replace("{rounds}", String(run.rounds || 0)));

  return (
    <details
      className={`agent-loop-step agent-loop-step--${status}`}
      data-agent-run-id={run.run_id}
    >
      <summary>
        <UserAvatar
          name={run.agent_name || run.service_key || "Agent"}
          avatarUrl={run.agent_avatar}
          type="agent"
          seed={run.agent_id || run.agent_subscription_id || run.run_id}
          size={20}
        />
        <strong className="agent-loop-step__name">
          {run.agent_name || run.service_key || "Agent"}
        </strong>
        <span className="agent-loop-step__activity">{activity}</span>
        <span className="agent-loop-step__status">
          <span
            className={`agent-loop-step__state${isRunning ? " agent-loop-step__state--running" : ""}`}
          />
          {statusLabel(status)}
        </span>
        <IconChevronDown className="agent-loop-step__chevron" size={14} />
      </summary>

      <div className="agent-loop-step__details">
        {run.objective && activeTool && (
          <div className="agent-loop-step__objective">{run.objective}</div>
        )}
        <div className="agent-loop-step__tools">
          {tools.length > 0 ? (
            tools.map((tool, index) => {
              const badge = runtimeToolBadge(tool.name);
              const detail = toolDetail(tool.arguments);
              const toolStatus = tool.status || "completed";
              const isToolRunning = toolStatus === "running";
              const isToolError = toolStatus === "error";
              return (
                <div
                  key={`${tool.seq ?? index}-${tool.name}`}
                  className="assistant-process-step assistant-process-step--minimal agent-loop-tool-row"
                >
                  {isToolRunning ? (
                    <span className="assistant-process-spinner" />
                  ) : (
                    <span
                      className={`assistant-process-step-icon${isToolError ? " assistant-process-step-icon--error" : ""}`}
                    />
                  )}
                  <span
                    className="agent-loop-tool__badge"
                    style={{
                      background: badge.bg,
                      borderColor: badge.border,
                      color: badge.color,
                    }}
                  >
                    {badge.label}
                  </span>
                  <span className="assistant-process-step-label agent-loop-tool-row__label">
                    {toolLabel(tool.name)}
                    {detail && (
                      <span className="agent-loop-tool-row__detail">
                        : {detail}
                      </span>
                    )}
                    {durationLabel(tool.duration_ms) && (
                      <span className="assistant-process-step-duration">
                        {durationLabel(tool.duration_ms)}
                      </span>
                    )}
                  </span>
                </div>
              );
            })
          ) : (
            <div className="agent-loop-step__empty">
              {t("component.workspace_chat.agent_run_preparing")}
            </div>
          )}
        </div>
        <div className="agent-loop-step__meta">
          {t("component.workspace_chat.agent_run_actions")
            .replace(
              "{count}",
              String(tools.length || run.tool_calls_made?.length || 0),
            )
            .replace("{rounds}", String(run.rounds || 0))}
        </div>
      </div>
    </details>
  );
}
