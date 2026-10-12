import { Link } from "react-router-dom";
import type { TaskBlocker } from "../../lib/types";
import { t } from "../../lib/i18n";
import { canRetryTaskBlocker, taskBlockerAction, taskBlockerView } from "../../lib/taskBlocker";
import Button from "../ui/Button";
import StatusPill from "../ui/StatusPill";

/** Shared, passive projection; only the existing recovery callback can retry. */
export default function TaskBlockerNotice({
  blocker, workspaceId, technicalReason, showActions = false, showStatus = true, onRetry, isPending = false,
}: {
  blocker: TaskBlocker;
  workspaceId?: string;
  technicalReason?: string;
  showActions?: boolean;
  showStatus?: boolean;
  onRetry?: () => void;
  isPending?: boolean;
}) {
  const view = taskBlockerView(blocker);
  const action = showActions ? taskBlockerAction(blocker, workspaceId) : null;
  return (
    <div className="task-blocker-notice" data-task-blocker={blocker.kind}>
      <div className="task-blocker-heading">
        {view.subject && <strong>{view.subject}</strong>}
        {showStatus && <StatusPill status={view.status} label={view.label} size="sm" />}
      </div>
      <p>{view.description}</p>
      {view.planningNote && <p className="task-blocker-note">{view.planningNote}</p>}
      {showActions && (action || (onRetry && canRetryTaskBlocker(blocker))) && (
        <div className="task-blocker-actions">
          {action && <Link className="btn-manor" to={action.href}>{action.label}</Link>}
          {onRetry && canRetryTaskBlocker(blocker) && (
            <Button variant="ghost" size="sm" onClick={onRetry} disabled={isPending}>
              {isPending ? t("component.task_recovery_panel.starting") : t("component.task_blocker.retry_after_setup")}
            </Button>
          )}
        </div>
      )}
      {(technicalReason || blocker.missing_capabilities.length > 0) && (
        <details className="task-blocker-technical">
          <summary>{t("component.task_blocker.technical_details")}</summary>
          {blocker.missing_capabilities.length > 0 && <code>{blocker.missing_capabilities.join(", ")}</code>}
          {technicalReason && <p>{technicalReason}</p>}
        </details>
      )}
    </div>
  );
}
