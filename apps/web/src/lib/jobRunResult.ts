type JsonRecord = Record<string, any>;

function isPlainRecord(value: unknown): value is JsonRecord {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function textValue(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value.trim();
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "";
}

function pushLine(lines: string[], label: string, value: unknown) {
  const text = textValue(value);
  if (text) lines.push(`${label}: ${text}`);
}

export function summarizeJobRunResult(result: unknown): string[] {
  if (!isPlainRecord(result)) return [];

  const lines: string[] = [];
  pushLine(lines, "Status", result.status);
  pushLine(lines, "Message", result.message || result.summary || result.result_summary);
  pushLine(lines, "Reason", result.reason);
  pushLine(lines, "Task", result.task_id);
  pushLine(lines, "Agent execution", result.agent_execution_id);

  const verdict = result.supervisor_verdict;
  if (isPlainRecord(verdict)) {
    pushLine(lines, "Supervisor", verdict.verdict);
    pushLine(lines, "Supervisor summary", verdict.summary);
    pushLine(lines, "Supervisor reason", verdict.reason);
  } else {
    pushLine(lines, "Supervisor", verdict);
  }

  const primitiveKeys = Object.keys(result).filter((key) => {
    if (
      key === "status"
      || key === "message"
      || key === "summary"
      || key === "result_summary"
      || key === "reason"
      || key === "task_id"
      || key === "agent_execution_id"
      || key === "supervisor_verdict"
      || key === "response"
      || key === "error"
    ) {
      return false;
    }
    const value = result[key];
    return (
      typeof value === "string"
      || typeof value === "number"
      || typeof value === "boolean"
    );
  });

  for (const key of primitiveKeys.slice(0, 8)) {
    pushLine(lines, key, result[key]);
  }

  if (!lines.length && result.response) {
    const response = textValue(result.response);
    if (response) lines.push(`Response: ${response.slice(0, 500)}${response.length > 500 ? "..." : ""}`);
  }

  return lines;
}

export function stringifyJobRunResult(result: unknown): string {
  if (result == null) return "";
  try {
    return JSON.stringify(result, null, 2);
  } catch {
    return String(result);
  }
}
