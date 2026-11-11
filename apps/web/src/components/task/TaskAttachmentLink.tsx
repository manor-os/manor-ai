import { useRef, useState, type CSSProperties, type ReactNode } from "react";
import { api, ApiError, parseTaskAttachmentUrl } from "../../lib/api";
import { saveFileResponse } from "../../lib/fileDownload";
import { t } from "../../lib/i18n";
import { useToastStore } from "../../stores/toast";
import Button from "../ui/Button";

/** Task uploads are protected API resources, not public navigation URLs. */
export default function TaskAttachmentLink({ href, filename, children, className = "", style }: {
  href?: string;
  filename?: string;
  children: ReactNode;
  className?: string;
  style?: CSSProperties;
}) {
  const [busy, setBusy] = useState(false);
  const active = useRef(false);
  const attachment = parseTaskAttachmentUrl(href);
  if (!attachment) return <a href={href} target="_blank" rel="noopener noreferrer" className={className} style={style}>{children}</a>;

  const download = async () => {
    if (active.current) return;
    active.current = true;
    setBusy(true);
    const toast = useToastStore.getState();
    const controller = new AbortController();
    const name = filename || attachment.filename;
    const toastId = `task-attachment-download:${Math.random().toString(36).slice(2)}`;
    toast.addToast({
      id: toastId, type: "info", title: t("download.in_progress"), message: name, duration: 0,
      actionLabel: t("common.cancel"), onAction: () => controller.abort(),
    });
    try {
      const result = await saveFileResponse(name,
        () => api.tasks.downloadAttachmentResponse(attachment.taskId, attachment.filename, { signal: controller.signal }),
        controller.signal);
      if (result === "saved") toast.success(t("download.saved"), name);
      if (result === "requested") toast.info(t("download.requested"), t("download.check_browser"));
    } catch (error) {
      if (!controller.signal.aborted) {
        toast.error(t("download.failed"), error instanceof ApiError
          ? `${error.message} (HTTP ${error.status})`
          : error instanceof Error ? error.message : undefined);
      }
    } finally {
      toast.removeToast(toastId);
      active.current = false;
      setBusy(false);
    }
  };

  return <Button variant="ghost" className={`task-attachment-link ${className}`} loading={busy} ariaBusy={busy}
    style={{ height: "auto", padding: 0, maxWidth: "100%", font: "inherit", textAlign: "left", overflowWrap: "anywhere", ...style }}
    onClick={download}>
    {children}
  </Button>;
}
