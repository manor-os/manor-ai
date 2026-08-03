import {
  useEffect,
  useId,
  useMemo,
  useState,
  type ComponentType,
  type ReactNode,
} from "react";
import {
  IconArrowRight,
  IconCheck,
  IconChevronDown,
  IconClock,
  IconClose,
  IconError,
  IconExternalLink,
  IconFlow,
  IconList,
  IconPause,
  IconPlay,
  type IconProps,
} from "../icons";
import { t } from "../../lib/i18n";
import {
  currentNodeIndex,
  isWorkflowRunActive,
  notReachedNodeCount,
  progressNodeCount,
  processedNodeCount,
  workflowCurrentStepLabelKey,
  workflowProgressNodes,
  workflowRunStatusPresentation,
  type WorkflowRunNode,
  type WorkflowRunStatusLabelKey,
  type WorkflowRunStatus,
  type WorkflowRunView,
} from "./workflowRunDisplay";

const LONG_NODE_LIST_THRESHOLD = 10;
const UPCOMING_NODE_COUNT = 2;

const STATUS_ICONS: Record<WorkflowRunStatus, ComponentType<IconProps>> = {
  pending: IconClock,
  running: IconPlay,
  completed: IconCheck,
  paused: IconPause,
  failed: IconError,
  skipped: IconArrowRight,
  cancelled: IconClose,
};
function statusLabel(status: WorkflowRunStatusLabelKey): string {
  return t(`component.workflow_run.status.${status}`);
}

function formatElapsed(milliseconds: number): string {
  const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
  const seconds = totalSeconds % 60;
  const totalMinutes = Math.floor(totalSeconds / 60);
  const minutes = totalMinutes % 60;
  const hours = Math.floor(totalMinutes / 60);
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
    : `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function elapsedMilliseconds(run: WorkflowRunView, now: number): number {
  if (typeof run.elapsedMs === "number" && Number.isFinite(run.elapsedMs)) {
    return Math.max(0, run.elapsedMs);
  }
  const started = run.startedAt ? Date.parse(run.startedAt) : Number.NaN;
  if (!Number.isFinite(started)) return 0;
  const completed = run.completedAt ? Date.parse(run.completedAt) : Number.NaN;
  const end = Number.isFinite(completed) ? completed : now;
  return Math.max(0, end - started);
}

export function WorkflowNodeStatusIcon({ node }: { node: WorkflowRunNode }) {
  const StatusIcon = STATUS_ICONS[node.status];
  return (
    <span className="workflow-run-node-icon" aria-hidden="true">
      <StatusIcon size={12} />
    </span>
  );
}

export interface WorkflowRunProgressProps {
  run: WorkflowRunView;
  className?: string;
  workflowHref?: string;
  historyHref?: string;
  headerAction?: ReactNode;
}

export default function WorkflowRunProgress({
  run,
  className = "",
  workflowHref,
  historyHref,
  headerAction,
}: WorkflowRunProgressProps) {
  const [expanded, setExpanded] = useState(false);
  const [showAllNodes, setShowAllNodes] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const listId = useId();
  const nodes = run.nodes;
  const progressNodes = workflowProgressNodes(nodes);
  const activeIndex = currentNodeIndex(run);
  const processed = processedNodeCount(progressNodes);
  const progressTotal = progressNodeCount(progressNodes);
  const notReached = notReachedNodeCount(progressNodes);
  const progressMaximum = Math.max(progressTotal, 1);
  const progress = processed / Math.max(progressTotal, 1);
  const progressPercent = Math.min(100, Math.max(0, progress * 100));
  const progressValueText = [
    t("component.workflow_run.executed", { count: processed }),
    t("component.workflow_run.not_reached", { count: notReached }),
  ].join(", ");
  const currentNode = nodes[activeIndex];
  const currentStepLabelKey = workflowCurrentStepLabelKey(run);
  const statusPresentation = workflowRunStatusPresentation(run);
  const compactNodeRows = useMemo(() => nodes.map((node, index) => ({ node, index })).filter(
    ({ node, index }) => (
      nodes.length <= LONG_NODE_LIST_THRESHOLD
      || showAllNodes
      || node.status !== "pending"
      || index === activeIndex
      || (index > activeIndex && index <= activeIndex + UPCOMING_NODE_COUNT)
    ),
  ), [activeIndex, nodes, showAllNodes]);
  const hiddenNodeCount = Math.max(0, nodes.length - compactNodeRows.length);

  useEffect(() => {
    setExpanded(false);
    setShowAllNodes(false);
  }, [run.id]);

  useEffect(() => {
    if (!isWorkflowRunActive(run)) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [run.status]);

  const elapsed = formatElapsed(elapsedMilliseconds(run, now));
  const expandLabel = expanded
    ? t("component.workflow_run.hide_all_steps")
    : t("component.workflow_run.show_all_steps");
  const toggleExpanded = () => {
    setExpanded((current) => {
      if (current) setShowAllNodes(false);
      return !current;
    });
  };

  return (
    <section
      className={`workflow-run-progress ${className}`.trim()}
      data-status={statusPresentation.labelKey}
      aria-label={t("component.workflow_run.progress")}
    >
      <header className="workflow-run-header">
        <div className="workflow-run-title">
          <span className="workflow-run-symbol" aria-hidden="true">
            <IconFlow size={14} />
          </span>
          <div className="workflow-run-identity">
            <strong>{run.title}</strong>
            <span className="workflow-run-current">
              <span className="workflow-run-current-label">
                {t(`component.workflow_run.${currentStepLabelKey}`)}
              </span>
              <strong>{currentNode?.name || run.title}</strong>
            </span>
          </div>
        </div>
        <div className="workflow-run-header-actions">
          {workflowHref && (
            <a
              className="workflow-run-header-link"
              href={workflowHref}
              aria-label={t("component.workflow_run.open_definition")}
              title={t("component.workflow_run.open_definition")}
            >
              <IconExternalLink size={13} aria-hidden="true" />
            </a>
          )}
          {historyHref && (
            <a
              className="workflow-run-header-link"
              href={historyHref}
              aria-label={t("component.workflow_run.open_history")}
              title={t("component.workflow_run.open_history")}
            >
              <IconList size={14} aria-hidden="true" />
            </a>
          )}
          <span className="workflow-run-status" data-status={statusPresentation.labelKey} role="status">
            <span
              className="workflow-run-status-indicator"
              data-motion={statusPresentation.motion}
              aria-hidden="true"
            />
            {statusLabel(statusPresentation.labelKey)}
          </span>
          {headerAction}
        </div>
      </header>

      <div className="workflow-run-summary">
        <span
          className="workflow-run-progress-bar"
          role="progressbar"
          aria-label={t("component.workflow_run.processed", { count: processed, total: progressTotal })}
          aria-valuemin={0}
          aria-valuemax={progressMaximum}
          aria-valuenow={processed}
          aria-valuetext={progressValueText}
        >
          <span style={{ width: `${progressPercent}%` }} />
        </span>
        <button
          type="button"
          className="workflow-run-summary-button"
          onClick={toggleExpanded}
          aria-expanded={expanded}
          aria-controls={listId}
          aria-label={expandLabel}
          title={expandLabel}
        >
          <span className="workflow-run-meta mono">
            <span>{t("component.workflow_run.executed", { count: processed })}</span>
            <span>{t("component.workflow_run.not_reached", { count: notReached })}</span>
            <span>{elapsed}</span>
            <IconChevronDown size={14} aria-hidden="true" />
          </span>
        </button>
      </div>

      {expanded && (
        <div className="workflow-run-node-list-shell" id={listId}>
          <ol className="workflow-run-node-list">
            {compactNodeRows.map(({ node, index }) => (
              <li key={node.id} aria-current={index === activeIndex ? "step" : undefined}>
                <span className="workflow-run-node-number mono">{index + 1}</span>
                <WorkflowNodeStatusIcon node={node} />
                <span className="workflow-run-node-name">{node.name}</span>
                <span className="workflow-run-node-status">{statusLabel(node.status)}</span>
              </li>
            ))}
          </ol>
          {nodes.length > LONG_NODE_LIST_THRESHOLD
            && (showAllNodes || hiddenNodeCount > 0) && (
            <button
              type="button"
              className="workflow-run-node-list-toggle"
              onClick={() => setShowAllNodes((current) => !current)}
              aria-expanded={showAllNodes}
            >
              {showAllNodes
                ? t("component.workflow_run.hide_remaining_steps")
                : t("component.workflow_run.show_remaining_steps", { count: hiddenNodeCount })}
            </button>
          )}
        </div>
      )}
    </section>
  );
}
