/**
 * Promote (Workspace simulation → live): the one confirmation, from chat or
 * from the Workspace's settings.
 *
 * What still needs connecting is listed with the same Connect row used
 * everywhere. Manor can ask for connections when work needs them. Required
 * Workspace setup is still checked before going live.
 */
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import Modal from "../ui/Modal";
import Button from "../ui/Button";
import LoadingSpinner from "../ui/LoadingSpinner";
import IntegrationConnectRow from "../integrations/IntegrationConnectRow";
import { api, type UnmetRequirement } from "../../lib/api";
import { useToastStore } from "../../stores/toast";
import { t } from "../../lib/i18n";
import "./PromoteWorkspaceModal.css";

interface Props {
  open: boolean;
  onClose: () => void;
  workspaceId: string;
  workspaceName?: string;
  onPromoted?: () => void;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** What live running still needs, each with its own way to connect or finish it. */
export function PromoteRequirementList({ workspaceId, requirements }: {
  workspaceId: string;
  requirements: UnmetRequirement[];
}) {
  const queryClient = useQueryClient();
  const recheck = () => {
    void queryClient.invalidateQueries({ queryKey: ["promote-preflight", workspaceId] });
  };
  return (
    <div className="promote-requirements">
      <div className="promote-requirements__title">
        {t("component.promote_workspace_modal.still_needed", { count: requirements.length })}
      </div>
      <ul className="integration-connect-list promote-requirements__list">
        {requirements.map((requirement) => {
          const key = `${requirement.kind}:${requirement.detail}`;
          const payload = requirement.payload || {};
          if (requirement.kind === "channel" && text(payload.channel_type)) {
            return (
              <IntegrationConnectRow
                key={key}
                workspaceId={workspaceId}
                requirement={{ kind: "channel", provider: text(payload.channel_type) }}
                hint={text(payload.purpose) || undefined}
                onConnected={recheck}
              />
            );
          }
          if (requirement.kind === "browser_session" && text(payload.provider)) {
            return (
              <IntegrationConnectRow
                key={key}
                workspaceId={workspaceId}
                requirement={{ kind: "browser_session", provider: text(payload.provider), label: text(payload.label) || undefined }}
                onConnected={recheck}
              />
            );
          }
          return (
            <li key={key} className="promote-requirements__setup">
              <span>{requirement.detail}</span>
              <Link to={`/workspaces/${encodeURIComponent(workspaceId)}`}>
                {t("component.promote_workspace_modal.finish_setup")}
              </Link>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export default function PromoteWorkspaceModal({ open, onClose, workspaceId, workspaceName, onPromoted }: Props) {
  const queryClient = useQueryClient();
  const toast = useToastStore();

  const { data: unmet, isLoading: preflightLoading } = useQuery({
    queryKey: ["promote-preflight", workspaceId],
    queryFn: () => api.workspaces.promotePreflight(workspaceId),
    enabled: open,
    staleTime: 0,
  });
  const missing = unmet ?? [];

  const promoteMutation = useMutation({
    mutationFn: () => api.workspaces.promote(workspaceId, { defer_connections: true }),
    onSuccess: (resp) => {
      if (!resp.promoted) {
        queryClient.setQueryData(["promote-preflight", workspaceId], resp.unmet);
        toast.error(t("component.promote_workspace_modal.promote_failed"));
        return;
      }
      for (const queryKey of [
        ["workspace", workspaceId],
        ["workspaces"],
        ["workspace-simulation-run", workspaceId],
        ["workspace-chat", workspaceId],
        ["workspace-app-view", workspaceId],
        ["simulation-report", workspaceId],
      ]) {
        void queryClient.invalidateQueries({ queryKey });
      }
      toast.success(t("component.promote_workspace_modal.workspace_promoted_to_live"));
      onPromoted?.();
      onClose();
    },
    onError: (e: Error) => toast.error(t("component.promote_workspace_modal.promote_failed"), e.message),
  });

  return (
    <Modal
      open={open}
      onClose={promoteMutation.isPending ? () => {} : onClose}
      title={workspaceName
        ? t("component.promote_workspace_modal.title_named", { name: workspaceName })
        : t("component.promote_workspace_modal.promote_to_live")}
      maxWidth="460px"
      footer={
        <>
          <Button variant="outline" onClick={onClose} disabled={promoteMutation.isPending}>{t("action.cancel")}</Button>
          <Button
            variant="primary"
            loading={promoteMutation.isPending}
            disabled={preflightLoading}
            onClick={() => promoteMutation.mutate()}
          >
            {t("component.promote_workspace_modal.promote_to_live")}
          </Button>
        </>
      }
    >
      <div className="promote-workspace">
        <p className="promote-workspace__lead">{t("component.promote_workspace_modal.keeps_setup")}</p>
        <ul className="promote-workspace__effects">
          <li>{t("component.promote_workspace_modal.effect_real_work")}</li>
          <li>{t("component.promote_workspace_modal.effect_approvals")}</li>
          <li>{t("component.promote_workspace_modal.effect_usage")}</li>
        </ul>
        {preflightLoading ? (
          <div className="promote-workspace__loading"><LoadingSpinner /></div>
        ) : missing.length > 0 ? (
          <>
            <PromoteRequirementList workspaceId={workspaceId} requirements={missing} />
            <p className="promote-workspace__note">{t("component.promote_workspace_modal.connect_later")}</p>
          </>
        ) : null}
      </div>
    </Modal>
  );
}
