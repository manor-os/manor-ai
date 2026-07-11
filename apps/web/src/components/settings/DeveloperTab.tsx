/**
 * Settings → Developer tab.
 *
 * Tenant-facing, self-serve programmable surface:
 *   1. LLM API keys (BYOK)        — reuses the /api-keys page, embedded
 *   2. Webhooks                   — reuses the /webhooks page, embedded
 *   3. Workers                    — external worker fleet
 *
 * Deployment-level credentials (model provider tokens, integration OAuth
 * clients) intentionally live in the platform admin portal, not here.
 *
 * Currently hidden behind SHOW_DEVELOPER_TAB in pages/Settings.tsx.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../../lib/api";
import type {
  WorkerResponse,
} from "../../lib/api";
import { t } from "../../lib/i18n";
import ApiKeys from "../../pages/ApiKeys";
import WebhookManager from "../../pages/WebhookManager";
import Button from "../ui/Button";
import LoadingSpinner from "../ui/LoadingSpinner";
import StatusPill from "../ui/StatusPill";
import { useAuthStore } from "../../stores/auth";
import { useToastStore } from "../../stores/toast";

export default function DeveloperTab() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 36 }}>
      <section>
        <ApiKeys embedded />
      </section>
      <section>
        <WebhookManager embedded />
      </section>
      <section>
        <WorkersSection />
      </section>
    </div>
  );
}

function WorkersSection() {
  const qc = useQueryClient();
  const toastSuccess = useToastStore((s) => s.success);
  const toastError = useToastStore((s) => s.error);
  const authToken = useAuthStore((s) => s.token);
  const authLoading = useAuthStore((s) => s.isLoading);
  const privateApiEnabled = !authLoading && Boolean(authToken);

  const workers = useQuery({
    queryKey: ["developer-workers"],
    queryFn: () => api.workers.list(),
    enabled: privateApiEnabled,
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ["developer-workers"] });

  const pause = useMutation({
    mutationFn: (id: string) => api.workers.pause(id),
    onSuccess: refresh,
  });
  const resume = useMutation({
    mutationFn: (id: string) => api.workers.resume(id),
    onSuccess: refresh,
  });
  const revoke = useMutation({
    mutationFn: (id: string) => api.workers.revoke(id),
    onSuccess: refresh,
  });

  function copy(text: string) {
    navigator.clipboard?.writeText(text).then(
      () => toastSuccess(t("page.developer.copied")),
      () => toastError(t("page.developer.copy_failed")),
    );
  }

  const items = workers.data || [];

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12, gap: 12 }}>
        <div>
          <h3 className="manor-section-title" style={{ margin: "0 0 4px" }}>{t("page.developer.workers_title")}</h3>
          <p style={{ margin: 0, fontSize: 12, color: "var(--ink-3, #78716c)" }}>
            {t("page.developer.workers_subtitle")}
          </p>
        </div>
      </div>


      {workers.isLoading && (
        <div style={{ display: "flex", justifyContent: "center", padding: "32px 0" }}>
          <LoadingSpinner size={24} />
        </div>
      )}

      {!workers.isLoading && items.length === 0 && (
        <div className="glass-card" style={{ padding: 24, textAlign: "center", fontSize: 13, color: "var(--ink-3, #78716c)" }}>
          {t("page.developer.no_workers")}
        </div>
      )}

      {!workers.isLoading && items.length > 0 && (
        <div className="glass-card" style={{ overflow: "hidden", padding: 0 }}>
          <div style={{ overflowX: "auto" }}>
            <table className="glass-table" style={{ width: "100%", fontSize: 13 }}>
              <thead>
                <tr>
                  <th>{t("page.developer.worker_name")}</th>
                  <th>{t("page.developer.worker_kind")}</th>
                  <th>{t("page.developer.worker_status")}</th>
                  <th>{t("page.developer.worker_last_seen")}</th>
                  <th style={{ textAlign: "right" }}>{t("page.developer.worker_actions")}</th>
                </tr>
              </thead>
              <tbody>
                {items.map((w: WorkerResponse) => (
                  <tr key={w.id}>
                    <td>
                      <div style={{ fontWeight: 600 }}>{w.display_name}</div>
                      <div className="mono" style={{ fontSize: 11, color: "var(--ink-3, #78716c)" }}>{w.id}</div>
                    </td>
                    <td>{w.kind}</td>
                    <td><StatusPill status={w.status} size="sm" /></td>
                    <td className="mono" style={{ fontSize: 12 }}>
                      {w.last_heartbeat_at ? new Date(w.last_heartbeat_at).toLocaleString() : "—"}
                    </td>
                    <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                      {w.status === "active" && (
                        <Button size="sm" variant="ghost" onClick={() => pause.mutate(w.id)} loading={pause.isPending}>
                          {t("page.developer.worker_pause")}
                        </Button>
                      )}
                      {w.status === "paused" && (
                        <Button size="sm" variant="ghost" onClick={() => resume.mutate(w.id)} loading={resume.isPending}>
                          {t("page.developer.worker_resume")}
                        </Button>
                      )}
                      {w.status !== "revoked" && (
                        <Button
                          size="sm"
                          variant="danger"
                          style={{ marginLeft: 6 }}
                          onClick={() => {
                            if (window.confirm(t("page.developer.worker_revoke_confirm", { name: w.display_name }))) {
                              revoke.mutate(w.id);
                            }
                          }}
                          loading={revoke.isPending}
                        >
                          {t("page.developer.worker_revoke")}
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
