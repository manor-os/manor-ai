import { t } from "../../lib/i18n";
import {
  workflowActivityCounts,
  workflowActivityText,
  type WorkflowStepActivity,
} from "./workflowRunDisplay";

/** What a running step's long work is doing now, with any warnings it raised. */
export default function WorkflowStepActivityLine({
  activity,
  className = "",
}: {
  activity: WorkflowStepActivity;
  className?: string;
}) {
  const { current } = activity;
  const text = workflowActivityText(current, t);
  const counts = workflowActivityCounts(current);
  const notices = activity.notices
    .map((notice) => ({ kind: notice.kind, text: workflowActivityText(notice, t) }))
    .filter((notice) => notice.text && notice.text !== text);
  if (!text && !counts) return null;
  return (
    <div
      className={`workflow-step-activity ${className}`.trim()}
      role="status"
      aria-live="polite"
      aria-label={t("component.workflow_run.activity.label")}
    >
      <span className="workflow-step-activity__now" data-kind={current.kind}>
        <span className="workflow-step-activity__text">{text}</span>
        {counts && <span className="workflow-step-activity__counts mono">{counts}</span>}
      </span>
      {typeof current.percent === "number" && (
        <span className="workflow-step-activity__bar" aria-hidden="true">
          <span style={{ width: `${Math.min(100, current.percent)}%` }} />
        </span>
      )}
      {notices.length > 0 && (
        <ul className="workflow-step-activity__notices">
          {notices.map((notice, index) => (
            <li key={`${index}-${notice.text}`} data-kind={notice.kind}>{notice.text}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
