import type { ReactNode } from "react";
import { formatFileSize } from "../../lib/format";
import { t } from "../../lib/i18n";
import { IconCheck, IconDocument } from "../icons";
import LoadingSpinner from "../ui/LoadingSpinner";

export type WorkflowUploadStatus = {
  phase: "queued" | "uploading" | "processing" | "uploaded" | "failed";
  percent?: number;
  error?: string;
};

export default function WorkflowUploadProgress({ file, status, children }: {
  file: File;
  status: WorkflowUploadStatus;
  children?: ReactNode;
}) {
  const active = status.phase === "uploading" || status.phase === "processing";
  const percent = Math.max(0, Math.min(100, status.percent || 0));
  const label = t(`component.workflow_upload.${status.phase}`);
  return <div className={`workflow-upload-row is-${status.phase}`} role="group" aria-label={file.name}>
    <span className="workflow-upload-icon" aria-hidden="true">
      {active ? <LoadingSpinner size={16} /> : status.phase === "uploaded" ? <IconCheck size={16} /> : <IconDocument size={16} />}
    </span>
    <div className="workflow-upload-details">
      <div className="workflow-upload-heading">
        <span className="workflow-upload-name" title={file.name}>{file.name}</span>
        <span className="workflow-upload-size">{formatFileSize(file.size)}</span>
      </div>
      <div className="workflow-upload-status"><span>{label}</span>{status.phase === "uploading" && <span>{percent}%</span>}</div>
      {active && <div className="workflow-upload-track" role="progressbar" aria-label={file.name}
        aria-valuemin={0} aria-valuemax={100} aria-valuenow={status.phase === "uploading" ? percent : undefined}
        aria-valuetext={status.phase === "processing" ? label : `${percent}%`}>
        <span style={{ width: `${status.phase === "processing" ? 100 : percent}%` }} />
      </div>}
      {status.error && <span className="workflow-upload-error">{status.error}</span>}
    </div>
    {children}
  </div>;
}
