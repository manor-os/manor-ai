import type { ReactNode } from "react";

/**
 * Keep a burst of tool calls quiet: say what was done, not a failure tally.
 * Each step still shows its own state when the group is opened.
 */
export default function ToolActivityGroup({
  summary,
  runningLabel,
  liveDetail,
  children,
}: {
  summary: string;
  runningLabel?: string;
  /** What a call still being written contains right now, shown while closed. */
  liveDetail?: string;
  children: ReactNode;
}) {
  return (
    <>
      <details className="assistant-tool-activity">
        <summary>
          {runningLabel && <span className="assistant-process-spinner" aria-hidden="true" />}
          {/* While a step runs, say which one; once done, summarize the group. */}
          <span className="assistant-tool-activity-title" title={runningLabel || summary}>{runningLabel || summary}</span>
          <svg className="assistant-tool-activity-chevron" aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <polyline points="9 5 16 12 9 19" />
          </svg>
        </summary>
        <div className="assistant-tool-activity-body">{children}</div>
      </details>
      {liveDetail && <div className="assistant-process-step-draft assistant-tool-activity-draft">{liveDetail}</div>}
    </>
  );
}
