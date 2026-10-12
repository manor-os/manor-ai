import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../../lib/api";
import { approvalKeys } from "../../lib/approvalQueries";
import { friendlyApprovalActionLabel } from "../../lib/approvalCopy";
import type { ApprovalCommandProjection } from "../../lib/approvalTypes";
import { t } from "../../lib/i18n";
import { useAuthStore } from "../../stores/auth";
import Button from "../ui/Button";
import Modal from "../ui/Modal";
import RadioCard from "../ui/RadioCard";
import "./approvals.css";

/** Only server-supported origins can atomically decide and save a rule. */
export default function ApprovalSaveRuleAction({ requestId, scopeLabel, disabled, onCompleted }: {
  requestId: string; scopeLabel?: string; disabled?: boolean; onCompleted: () => void;
}) {
  const user = useAuthStore(state => state.user);
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["approval-command", user?.entity_id, user?.id, requestId],
    queryFn: () => api.approvalCommands.get(requestId),
    enabled: !!user && !!requestId && !disabled, retry: false, staleTime: 10_000,
  });
  const [preview, setPreview] = useState<ApprovalCommandProjection | null>(null);
  const [selected, setSelected] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const idempotency = useRef("");
  useEffect(() => { setPreview(null); setSelected(""); setError(""); setConflict(false); idempotency.current = ""; }, [user?.id, user?.entity_id, requestId]);
  const canSave = (value?: ApprovalCommandProjection | null) => value?.available_decisions?.includes("approve_and_save_rule") && value.scope && value.policy_version && value.rule_options.length > 0;

  const showPreview = (value: ApprovalCommandProjection) => {
    setPreview(value); setSelected(value.rule_options[0]?.id || "");
    idempotency.current = crypto.randomUUID(); setConflict(false); setError("");
  };
  const refresh = async () => {
    setBusy(true);
    try {
      const value = await query.refetch({ throwOnError: true });
      if (value.data && canSave(value.data)) showPreview(value.data);
      else setError(t("approval_settings.unavailable"));
    } catch { setError(t("approval_settings.error")); }
    finally { setBusy(false); }
  };
  const save = async () => {
    if (!preview || !canSave(preview) || !selected || busy || conflict || disabled) return;
    setBusy(true); setError("");
    try {
      const result = await api.approvalCommands.saveRule({
        request: preview.request_ref, choice: "approve_and_save_rule",
        expectedDigest: preview.digest, idempotencyKey: idempotency.current,
        expectedPolicyVersion: preview.policy_version!, optionId: selected,
      });
      if (user) queryClient.setQueryData(approvalKeys.settings(result.changed_scope.entity_id || user.entity_id, user.id, result.changed_scope), result.policy);
      void queryClient.invalidateQueries({ queryKey: approvalKeys.entity(result.changed_scope.entity_id || user?.entity_id || "") });
      void queryClient.invalidateQueries({ queryKey: ["approval-command"] });
      void queryClient.invalidateQueries({ queryKey: ["workspace-chat"] });
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
      setPreview(null); onCompleted();
    } catch (cause) {
      if (cause instanceof ApiError && [403, 404, 409].includes(cause.status)) {
        setConflict(true);
        setError(t(cause.status === 409 ? "approval_settings.command_conflict" : "approval_settings.unavailable"));
      } else setError(t("approval_settings.save_error"));
    } finally { setBusy(false); }
  };
  if (!preview && !canSave(query.data)) return null;
  const scope = preview?.scope;
  return <>
    <Button size="sm" variant="outline" disabled={disabled || busy || !canSave(query.data)} onClick={() => query.data && showPreview(query.data)}>
      {t("approval_settings.approve_save_rule")}
    </Button>
    <Modal open={!!preview} onClose={() => { if (!busy) setPreview(null); }} title={t("approval_settings.approve_save_rule")} maxWidth="520px" footer={<>
      <Button variant="outline" disabled={busy} onClick={() => setPreview(null)}>{t("common.cancel")}</Button>
      <Button loading={busy} disabled={disabled || conflict || !selected || !canSave(preview)} onClick={() => void save()}>{t("approval_settings.approve_save_rule")}</Button>
    </>}>
      <p className="approval-note">{t("approval_settings.command_description")}</p>
      {scope && <p className="approval-command-scope">{t("approval_settings.scope_prefix", { scope: `${t(`approval_settings.${scope.kind}`)} · ${scopeLabel || scope.id || ""}` })}</p>}
      <div className="approval-command-options" role="radiogroup" aria-label={t("approval_settings.selector")}>
        {preview?.rule_options.map(option => <RadioCard key={option.id} variant="tile" selected={selected === option.id} disabled={busy || conflict} onSelect={() => { setSelected(option.id); idempotency.current = crypto.randomUUID(); }} ariaLabel={friendlyApprovalActionLabel(option.action_key)}>
          <strong>{friendlyApprovalActionLabel(option.action_key)}</strong>
          <span className="approval-mode-description">{t(option.coverage === "exact_resource" ? "approval_settings.command_exact_targets" : "approval_settings.command_all_targets")}</span>
          {option.rule.selector.kind === "resource" && <span className="approval-mode-description">{[option.rule.selector.account_id, ...option.rule.selector.resource_ids].filter(Boolean).join(" · ")}</span>}
        </RadioCard>)}
      </div>
      {error && <div className="approval-status" role="alert"><p>{error}</p>{conflict && <Button size="sm" variant="outline" disabled={busy} onClick={() => void refresh()}>{t("approval_settings.command_refresh")}</Button>}</div>}
    </Modal>
  </>;
}
