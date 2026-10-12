import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import { useToastStore } from "../../stores/toast";
import Button from "../ui/Button";
import Modal from "../ui/Modal";

interface Props {
  open: boolean;
  onClose: () => void;
  workspaceId: string;
  canPromote: boolean;
}

/** A portable simulation review surface; it owns no simulation state itself. */
export default function SimulationPromotionPanel({ open, onClose, workspaceId, canPromote }: Props) {
  const toast = useToastStore();
  const queryClient = useQueryClient();
  const report = useQuery({
    queryKey: ["workspace-simulation-report", workspaceId],
    queryFn: () => api.workspaces.simulationReport(workspaceId),
    enabled: open,
  });
  const preflight = useQuery({
    queryKey: ["workspace-promote-preflight", workspaceId],
    queryFn: () => api.workspaces.promotePreflight(workspaceId),
    enabled: open,
  });
  const promote = useMutation({
    mutationFn: () => api.workspaces.promote(workspaceId),
    onSuccess: (result) => {
      if (!result.promoted) return;
      queryClient.invalidateQueries({ queryKey: ["workspace", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      toast.success(t("page.workspace_detail.promote_to_live"));
      onClose();
    },
    onError: (error: Error) => toast.error(error.message),
  });
  const unmet = preflight.data ?? [];
  const ready = preflight.isSuccess && unmet.length === 0;

  return (
    <Modal
      open={open}
      onClose={promote.isPending ? () => {} : onClose}
      title={t("page.simulation_report.title")}
      maxWidth="560px"
      footer={(
        <>
          <Button variant="outline" onClick={onClose} disabled={promote.isPending}>{t("action.cancel")}</Button>
          {canPromote && <Button
              variant="primary"
              loading={promote.isPending}
              disabled={!ready || promote.isPending}
              onClick={() => {
                if (window.confirm(t("page.workspace_detail.promote_to_live"))) promote.mutate();
              }}
            >
              {t("page.workspace_detail.promote_to_live")}
            </Button>}
        </>
      )}
    >
      <div style={{ display: "grid", gap: 12, fontSize: 13 }}>
        {report.isLoading || preflight.isLoading ? <span>{t("page.simulation_report.loading")}</span> : null}
        {report.data && (
          <div style={{ display: "grid", gap: 4 }}>
            <strong>{report.data.workspace_name}</strong>
            <span>{report.data.activity.total_steps} {t("page.simulation_report.activity")}</span>
            <span>{report.data.cost.total_credits} {t("page.simulation_report.cost")}</span>
            {report.data.notes.map((note) => <span key={note}>{note}</span>)}
          </div>
        )}
        {report.isError && <span style={{ color: "var(--danger)" }}>{t("page.simulation_report.unavailable")}</span>}
        {preflight.isError && <span style={{ color: "var(--danger)" }}>{t("page.workspaces.import_failed")}</span>}
        {unmet.map((requirement) => (
          <span key={`${requirement.kind}:${requirement.detail}`} style={{ color: "var(--danger)" }}>
            {requirement.detail}
          </span>
        ))}
      </div>
    </Modal>
  );
}
