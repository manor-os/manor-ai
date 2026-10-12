import type { Workspace } from "../../lib/types";
import { t } from "../../lib/i18n";
import Button from "../ui/Button";
import Modal from "../ui/Modal";

export function workspaceUsageGuide(workspace?: Workspace | null): string {
  const blueprint = workspace?.settings?._blueprint;
  if (!blueprint || typeof blueprint !== "object") return "";
  const guide = (blueprint as Record<string, unknown>).usage_guide;
  return typeof guide === "string" ? guide.trim() : "";
}

export default function BlueprintGuideDialog({
  open,
  guide,
  workspaceName,
  onClose,
}: {
  open: boolean;
  guide: string;
  workspaceName: string;
  onClose: () => void;
}) {
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t("page.blueprint_detail.workspace_usage_guide", { name: workspaceName })}
      maxWidth="640px"
      footer={<Button variant="primary" onClick={onClose}>{t("action.close")}</Button>}
    >
      <div style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontSize: 13, lineHeight: 1.7, color: "var(--text-default)" }}>
        {guide}
      </div>
    </Modal>
  );
}
