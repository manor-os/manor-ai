import { useEffect, useMemo, useState } from "react";
import { useMutation } from "@tanstack/react-query";

import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import Button from "../ui/Button";
import Checkbox from "../ui/Checkbox";
import Modal from "../ui/Modal";

const LEDGER_OPTIONS = [
  {
    contractId: "manor.recruiting_ledger/v1",
    labelKey: "component.workspace_chat.recruiting_hr_ledger",
    descriptionKey: "component.workspace_chat.recruiting_hr_ledger_description",
  },
  {
    contractId: "manor.finance_ledger/v1",
    labelKey: "component.workspace_chat.finance_ledger",
    descriptionKey: "component.workspace_chat.finance_ledger_description",
  },
  {
    contractId: "manor.relationship_ledger/v1",
    labelKey: "component.workspace_chat.relationship_ledger",
    descriptionKey: "component.workspace_chat.relationship_ledger_description",
  },
  {
    contractId: "manor.content_ledger/v1",
    labelKey: "component.workspace_chat.content_ledger",
    descriptionKey: "component.workspace_chat.content_ledger_description",
  },
] as const;

interface WorkspaceLedgerConfigurationDialogProps {
  open: boolean;
  workspaceId: string;
  initialContractIds: string[];
  onClose: () => void;
  onConfigured: (contractIds: string[]) => void;
}

export default function WorkspaceLedgerConfigurationDialog({
  open,
  workspaceId,
  initialContractIds,
  onClose,
  onConfigured,
}: WorkspaceLedgerConfigurationDialogProps) {
  const initialKey = initialContractIds.join("|");
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(initialContractIds),
  );
  const selectedContractIds = useMemo(
    () => LEDGER_OPTIONS
      .map((option) => option.contractId)
      .filter((contractId) => selected.has(contractId)),
    [selected],
  );
  const saveMutation = useMutation({
    mutationFn: () => api.workspaces.configureLedgers(
      workspaceId,
      selectedContractIds,
    ),
    onSuccess: (result) => {
      onConfigured(
        result.ledger_contracts
          .map((item) => String(item.contract_id || ""))
          .filter(Boolean),
      );
    },
  });

  useEffect(() => {
    if (!open) return;
    setSelected(new Set(initialContractIds));
    saveMutation.reset();
    // initialKey is the stable representation needed to reset on a fresh
    // Workspace response without reacting to a new array identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialKey, open, workspaceId]);

  const close = () => {
    if (!saveMutation.isPending) onClose();
  };

  return (
    <Modal
      open={open}
      onClose={close}
      title={t("component.workspace_chat.configure_ledgers")}
      className="workspace-ledger-config-dialog"
      bodyClassName="workspace-ledger-config-dialog__body"
      maxWidth="560px"
      footer={(
        <>
          <Button variant="outline" onClick={close} disabled={saveMutation.isPending}>
            {t("action.cancel")}
          </Button>
          <Button
            onClick={() => saveMutation.mutate()}
            loading={saveMutation.isPending}
          >
            {t("action.save")}
          </Button>
        </>
      )}
    >
      <p className="workspace-ledger-config-dialog__description">
        {t("component.workspace_chat.ledger_configuration_description")}
      </p>
      <div className="workspace-ledger-config-options">
        {LEDGER_OPTIONS.map((option) => (
          <Checkbox
            key={option.contractId}
            checked={selected.has(option.contractId)}
            disabled={saveMutation.isPending}
            onChange={(checked) => {
              setSelected((current) => {
                const next = new Set(current);
                if (checked) next.add(option.contractId);
                else next.delete(option.contractId);
                return next;
              });
            }}
            className="workspace-ledger-config-option"
            label={(
              <span className="workspace-ledger-config-option__copy">
                <strong>{t(option.labelKey)}</strong>
                <small>{t(option.descriptionKey)}</small>
              </span>
            )}
          />
        ))}
      </div>
      {saveMutation.isError && (
        <p className="workspace-ledger-config-dialog__error" role="alert">
          {saveMutation.error instanceof Error
            ? saveMutation.error.message
            : t("component.workspace_chat.ledger_configuration_save_failed")}
        </p>
      )}
    </Modal>
  );
}
