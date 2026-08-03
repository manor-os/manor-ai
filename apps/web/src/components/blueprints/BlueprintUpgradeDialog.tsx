/**
 * Confirming a blueprint upgrade.
 *
 * An upgrade overwrites content the workspace is running, so what it will do
 * is shown item by item before anything is written — including the items it
 * will NOT touch, because those are the ones an operator most needs to know
 * about. "Update available" alone is not something anyone can act on.
 *
 * Undo is offered afterwards rather than promised in advance: only what the
 * upgrade actually overwrote can be put back.
 */
import { Fragment, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import Modal from "../ui/Modal";
import LoadingSpinner from "../ui/LoadingSpinner";
import { api } from "../../lib/api";
import { t } from "../../lib/i18n";

const ACTION_TONE: Record<string, { fg: string; bg: string }> = {
  update: { fg: "#3f6f68", bg: "#eaf1ef" },
  keep_yours: { fg: "#8a6d3b", bg: "#f6efe3" },
  unchanged: { fg: "#78716c", bg: "#f5f5f4" },
  missing: { fg: "#78716c", bg: "#f5f5f4" },
};

export interface BlueprintUpgradeDialogProps {
  open: boolean;
  onClose: () => void;
  workspaceId: string;
  workspaceName: string;
}

export default function BlueprintUpgradeDialog({
  open,
  onClose,
  workspaceId,
  workspaceName,
}: BlueprintUpgradeDialogProps) {
  const queryClient = useQueryClient();
  const [applied, setApplied] = useState<{ updated: number; kept: number } | null>(null);

  const { data: plan, isLoading } = useQuery({
    queryKey: ["blueprint-upgrade-plan", workspaceId],
    queryFn: () => api.workspaces.blueprintUpgradePlan(workspaceId),
    enabled: open,
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["workspaces"] });
    queryClient.invalidateQueries({ queryKey: ["workspace", workspaceId] });
    queryClient.invalidateQueries({ queryKey: ["blueprint-upgrade-plan", workspaceId] });
  };

  const applyMutation = useMutation({
    mutationFn: () => api.workspaces.applyBlueprintUpgrade(workspaceId),
    onSuccess: (result) => {
      setApplied({ updated: result.updated.length, kept: result.kept_yours.length });
      invalidate();
    },
  });

  const revertMutation = useMutation({
    mutationFn: () => api.workspaces.revertBlueprintUpgrade(workspaceId),
    onSuccess: () => {
      setApplied(null);
      invalidate();
      onClose();
    },
  });

  const items = plan?.items ?? [];
  const toUpdate = items.filter((item) => item.action === "update");
  const keptYours = items.filter((item) => item.action === "keep_yours");
  const busy = applyMutation.isPending || revertMutation.isPending;

  return (
    <Modal
      open={open}
      onClose={busy ? () => {} : onClose}
      title={t("page.blueprints.upgrade_title").replace("{workspace}", workspaceName)}
    >
      {isLoading ? (
        <LoadingSpinner />
      ) : applied ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <p style={{ fontSize: 13, color: "#44403c", lineHeight: 1.6, margin: 0 }}>
            {t("page.blueprints.upgrade_done")
              .replace("{updated}", String(applied.updated))
              .replace("{kept}", String(applied.kept))}
          </p>
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button
              onClick={() => revertMutation.mutate()}
              disabled={busy}
              style={secondaryButton}
            >
              {t("page.blueprints.upgrade_revert")}
            </button>
            <button onClick={onClose} disabled={busy} style={primaryButton}>
              {t("action.done")}
            </button>
          </div>
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          {items.length === 0 ? (
            <p style={{ fontSize: 13, color: "#78716c", margin: 0 }}>
              {t("page.blueprints.upgrade_nothing")}
            </p>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {items
                .filter((item) => item.action === "update" || item.action === "keep_yours")
                .map((item) => {
                  const tone = ACTION_TONE[item.action] || ACTION_TONE.unchanged;
                  return (
                    <div
                      key={`${item.kind}:${item.slug}`}
                      style={{
                        display: "flex", gap: 10, alignItems: "flex-start",
                        padding: "10px 12px", borderRadius: 12, background: "rgba(250,250,249,0.7)",
                      }}
                    >
                      <span style={{
                        flexShrink: 0, fontSize: 9.5, fontWeight: 850, letterSpacing: "0.04em",
                        textTransform: "uppercase", color: tone.fg, background: tone.bg,
                        borderRadius: 999, padding: "3px 8px", marginTop: 1,
                      }}>
                        {t(`page.blueprints.upgrade_action_${item.action}`)}
                      </span>
                      <div style={{ minWidth: 0 }}>
                        <div style={{ fontSize: 13, fontWeight: 700, color: "#292524" }}>
                          {item.name}
                          <span style={{ color: "#a8a29e", fontWeight: 600 }}> · {item.kind}</span>
                        </div>
                        {item.changes.length > 0 && (
                          <div style={{ fontSize: 11.5, color: "#78716c", marginTop: 2 }}>
                            {item.changes.join(" · ")}
                          </div>
                        )}
                        {Object.keys(item.new_content || {}).length > 0 && (
                          <details style={{ marginTop: 6 }}>
                            <summary style={{
                              cursor: "pointer", fontSize: 11.5, fontWeight: 700,
                              color: "#3f6f68", listStyle: "none",
                            }}>
                              {t("page.blueprints.upgrade_show_new")}
                            </summary>
                            {Object.entries(item.new_content).map(([field, value]) => (
                              <Fragment key={field}>
                                <div style={{
                                  fontSize: 10, fontWeight: 800, color: "#a8a29e",
                                  textTransform: "uppercase", letterSpacing: "0.05em",
                                  margin: "8px 0 3px",
                                }}>
                                  {field}
                                </div>
                                <pre style={{
                                  margin: 0, padding: "8px 10px", borderRadius: 9,
                                  background: "rgba(28,25,23,0.035)", color: "#44403c",
                                  fontSize: 11, lineHeight: 1.55, maxHeight: 220,
                                  overflow: "auto", whiteSpace: "pre-wrap",
                                  wordBreak: "break-word",
                                }}>
                                  {value}
                                </pre>
                              </Fragment>
                            ))}
                          </details>
                        )}
                      </div>
                    </div>
                  );
                })}
            </div>
          )}

          {keptYours.length > 0 && (
            <p style={{ fontSize: 12, color: "#8a6d3b", lineHeight: 1.55, margin: 0 }}>
              {t("page.blueprints.upgrade_keeps_yours").replace("{count}", String(keptYours.length))}
            </p>
          )}

          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button onClick={onClose} disabled={busy} style={secondaryButton}>
              {t("action.cancel")}
            </button>
            <button
              onClick={() => applyMutation.mutate()}
              disabled={busy || toUpdate.length === 0}
              style={{ ...primaryButton, opacity: toUpdate.length === 0 ? 0.5 : 1 }}
            >
              {t("page.blueprints.upgrade_confirm").replace("{count}", String(toUpdate.length))}
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}

const primaryButton: React.CSSProperties = {
  height: 32, padding: "0 14px", borderRadius: 10, border: "none",
  background: "#4f7d75", color: "#fff", fontSize: 12.5, fontWeight: 800, cursor: "pointer",
};

const secondaryButton: React.CSSProperties = {
  height: 32, padding: "0 14px", borderRadius: 10, border: "none",
  background: "#f5f5f4", color: "#57534e",
  fontSize: 12.5, fontWeight: 800, cursor: "pointer",
};
