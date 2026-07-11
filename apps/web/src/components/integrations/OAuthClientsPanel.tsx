import { useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { useAuthStore } from "../../stores/auth";
import GlassCard from "../ui/GlassCard";
import Button from "../ui/Button";
import Chip from "../ui/Chip";
import Modal from "../ui/Modal";
import { useToastStore } from "../../stores/toast";
import { IconExternalLink } from "../icons";
import { t } from "../../lib/i18n";


type OAuthClient = {
  server_key: string;
  name: string;
  client_id: string | null;
  has_secret: boolean;
  source: "env" | "ui" | "db" | "none";
  scopes: string | null;
  configured: boolean;
  client_id_env_var: string;
  client_secret_env_var: string;
  redirect_uri: string;
};

function formatClientIdForDisplay(client: OAuthClient): string | null {
  // Never reveal deployment/env bootstrapped client IDs in the table.
  if (client.source === "env") return null;
  if (!client.client_id) return null;
  return client.client_id.length > 32
    ? client.client_id.slice(0, 14) + "…" + client.client_id.slice(-8)
    : client.client_id;
}

/** Copy a provider's redirect URI to the clipboard. Exported (and returns
 *  whether a copy was attempted) so the click behaviour is unit-testable
 *  without a DOM. */
export function copyRedirectUri(uri: string | null | undefined): boolean {
  const value = (uri || "").trim();
  if (!value) return false;
  navigator.clipboard?.writeText(value);
  return true;
}

export default function OAuthClientsPanel({
  focusServerKey,
}: {
  focusServerKey?: string | null;
}) {
  const qc = useQueryClient();
  const toast = useToastStore();
  const authToken = useAuthStore((s) => s.token);
  const authLoading = useAuthStore((s) => s.isLoading);
  const privateApiEnabled = !authLoading && Boolean(authToken);
  const [editing, setEditing] = useState<OAuthClient | null>(null);
  const [healthResults, setHealthResults] = useState<Record<string, {
    ok: boolean; status_code: number | null; detail: string;
  } | "loading">>({});
  const rowRefs = useRef<Record<string, HTMLTableRowElement | null>>({});

  const { data: clients, isLoading } = useQuery({
    queryKey: ["admin-oauth-clients"],
    queryFn: () => api.admin.oauthClients.list(),
    enabled: privateApiEnabled,
  });

  const { data: mcpServers } = useQuery({
    queryKey: ["mcp-servers"],
    queryFn: () => api.integrations.mcpServers(),
    enabled: privateApiEnabled,
  });

  const docsByServerKey = useMemo(() => {
    const map = new Map<string, string>();
    for (const row of mcpServers || []) {
      const key = (row as any)?.server_key;
      const url = (row as any)?.docs_url;
      if (key && url) map.set(String(key), String(url));
    }
    return map;
  }, [mcpServers]);

  const normalizedFocus = useMemo(
    () => (focusServerKey || "").trim().toLowerCase(),
    [focusServerKey],
  );

  useEffect(() => {
    if (!normalizedFocus || !clients?.length) return;
    const target = clients.find((c) => c.server_key.toLowerCase() === normalizedFocus);
    if (!target) return;
    const row = rowRefs.current[target.server_key];
    row?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [clients, normalizedFocus]);

  const resetMutation = useMutation({
    mutationFn: (k: string) => api.admin.oauthClients.reset(k),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-oauth-clients"] });
      qc.invalidateQueries({ queryKey: ["mcp-servers"] });
      toast.success(t("page.file_viewer.reset"), t("component.oauth_clients_panel.reverts_to_env_on_next_restart"));
    },
    onError: (err: Error) => toast.error(t("component.oauth_clients_panel.reset_failed"), err.message),
  });

  const checkHealth = async (k: string) => {
    setHealthResults((s) => ({ ...s, [k]: "loading" }));
    try {
      const r = await api.admin.oauthClients.checkHealth(k);
      setHealthResults((s) => ({ ...s, [k]: r }));
    } catch (err: any) {
      setHealthResults((s) => ({
        ...s,
        [k]: { ok: false, status_code: null, detail: err?.message || "Probe failed" },
      }));
    }
  };

  return (
    <>
      <GlassCard>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ background: "#fafaf9", borderBottom: "1px solid rgba(28,25,23,0.06)" }}>
              <th style={th}>{t("page.api_keys.provider")}</th>
              <th style={th}>{t("component.oauth_clients_panel.client_id")}</th>
              <th style={th}>{t("component.oauth_clients_panel.secret")}</th>
              <th style={th}>{t("page.skills.source")}</th>
              <th style={th}>{t("page.agent_dashboard.status")}</th>
              <th style={{ ...th, textAlign: "right" }}>{t("page.custom_fields.actions")}</th>
            </tr>
          </thead>
          <tbody>
            {isLoading && (
              <tr><td colSpan={6} style={{ padding: 32, textAlign: "center", color: "#a8a29e" }}>
                {t("component.oauth_clients_panel.loading")}</td></tr>
            )}
            {clients?.map((c) => {
              const health = healthResults[c.server_key];
              const focused = normalizedFocus && c.server_key.toLowerCase() === normalizedFocus;
              return (
                <tr
                  key={c.server_key}
                  ref={(node) => { rowRefs.current[c.server_key] = node; }}
                  style={{
                    borderBottom: "1px solid #f5f5f4",
                    background: focused ? "#fafaf9" : "transparent",
                  }}
                >
                  <td style={td}>
                    <div style={{ fontWeight: 600, color: "#1c1917" }}>{c.name}</div>
                    <div style={{ fontSize: 11, color: "#a8a29e", fontFamily: "ui-monospace, monospace" }}>
                      {c.server_key}
                    </div>
                    {c.redirect_uri && (
                      <div
                        onClick={() => copyRedirectUri(c.redirect_uri)}
                        title={t("component.oauth_clients_panel.copy_redirect_uri")}
                        style={{
                          fontSize: 10,
                          color: "#78716c",
                          fontFamily: "ui-monospace, monospace",
                          marginTop: 4,
                          cursor: "pointer",
                          wordBreak: "break-all",
                          maxWidth: 280,
                        }}
                      >
                        ↳ {c.redirect_uri}
                      </div>
                    )}
                    {focused && (
                      <div style={{ fontSize: 10, color: "#78716c", marginTop: 4 }}>
                        {t("component.oauth_clients_panel.selected_from_integrations")}</div>
                    )}
                  </td>
                  <td style={{ ...td, fontFamily: "ui-monospace, monospace", fontSize: 11, color: "#57534e" }}>
                    {(() => {
                      const displayClientId = formatClientIdForDisplay(c);
                      if (displayClientId) return displayClientId;
                      if (c.source === "env") {
                        return <span style={{ color: "#a8a29e" }}>{t("component.oauth_clients_panel.system_default_hidden")}</span>;
                      }
                      return <span style={{ color: "#d6d3d1" }}>—</span>;
                    })()}
                  </td>
                  <td style={td}>
                    {c.has_secret
                      ? <Chip size="sm" variant="green">{t("component.oauth_clients_panel.set")}</Chip>
                      : <Chip size="sm" variant="slate">{t("component.oauth_clients_panel.missing")}</Chip>}
                  </td>
                  <td style={td}>
                    {c.source === "env" && <Chip size="sm" variant="slate">{t("component.oauth_clients_panel.env")}</Chip>}
                    {c.source === "ui" && <Chip size="sm" variant="purple">{t("component.oauth_clients_panel.override_source")}</Chip>}
                    {c.source === "db" && <Chip size="sm" variant="slate">{t("component.oauth_clients_panel.db")}</Chip>}
                    {c.source === "none" && <Chip size="sm" variant="slate">{t("component.oauth_clients_panel.unset")}</Chip>}
                  </td>
                  <td style={td}>
                    {c.configured
                      ? <Chip size="sm" variant="green">{t("component.oauth_clients_panel.configured")}</Chip>
                      : <Chip size="sm" variant="slate">{t("component.oauth_clients_panel.not_configured")}</Chip>}
                    {health === "loading" && (
                      <div style={{ fontSize: 10, color: "#a8a29e", marginTop: 4 }}>{t("component.oauth_clients_panel.probing")}</div>
                    )}
                    {health && health !== "loading" && (
                      <div style={{ fontSize: 10, marginTop: 4, color: health.ok ? "#44895f" : "#c14a44" }}>
                        {health.ok ? t("component.oauth_clients_panel.verified") : `✗ ${health.detail}`}
                      </div>
                    )}
                  </td>
                  <td style={{ ...td, textAlign: "right", whiteSpace: "nowrap" }}>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => checkHealth(c.server_key)}
                      disabled={!c.configured || health === "loading"}
                    >
                      {t("page.invoke_modal.test")}</Button>
                    <Button variant="outline" size="sm" onClick={() => setEditing(c)}>
                      {c.configured ? t("component.oauth_clients_panel.override") : t("page.apps.configure")}
                    </Button>
                    {c.source === "ui" && (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          if (confirm(`Reset ${c.name} OAuth override? Will fall back to env on next restart.`)) {
                            resetMutation.mutate(c.server_key);
                          }
                        }}
                        loading={resetMutation.isPending && resetMutation.variables === c.server_key}
                      >
                        {t("page.file_viewer.reset")}</Button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </GlassCard>

      <div style={{ fontSize: 11, color: "#a8a29e", marginTop: 12, lineHeight: 1.6 }}>
        <strong>{t("component.oauth_clients_panel.how_sources_work")}</strong> {t("component.oauth_clients_panel.startup_reads")}<code style={codeStyle}>{t("component.oauth_clients_panel.client_id_2")}</code> /
        <code style={codeStyle}>{t("component.oauth_clients_panel.client_secret")}</code> {t("component.oauth_clients_panel.and_seeds_them_into_db_source")}<code style={codeStyle}>env</code>{t("component.oauth_clients_panel.saving_here_overrides_them_source")}<code style={codeStyle}>override</code>{t("component.oauth_clients_panel.until_reset")}</div>

      {editing && (
        <EditModal
          client={editing}
          docsUrl={docsByServerKey.get(editing.server_key) || null}
          onClose={() => setEditing(null)}
          onSaved={() => {
            qc.invalidateQueries({ queryKey: ["admin-oauth-clients"] });
            qc.invalidateQueries({ queryKey: ["mcp-servers"] });
            setEditing(null);
            toast.success(t("page.blueprint_detail.saved"), t("component.oauth_clients_panel.oauth_credentials_encrypted"));
          }}
        />
      )}
    </>
  );
}

function EditModal({
  client, docsUrl, onClose, onSaved,
}: {
  client: OAuthClient;
  docsUrl?: string | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [scopes, setScopes] = useState("");
  const toast = useToastStore();

  const save = useMutation({
    mutationFn: () =>
      api.admin.oauthClients.update(client.server_key, {
        client_id: clientId.trim(),
        client_secret: clientSecret.trim(),
        scopes: scopes.trim() || undefined,
      }),
    onSuccess: onSaved,
    onError: (err: Error) => toast.error(t("page.blueprint_detail.save_failed"), err.message),
  });

  const valid = clientId.trim() && clientSecret.trim();

  return (
    <Modal open onClose={onClose} title={`${client.name} OAuth`} maxWidth="560px">
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        {docsUrl && (
          <a
            href={docsUrl}
            target="_blank"
            rel="noreferrer"
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              fontSize: 12,
              fontWeight: 600,
              color: "#78716c",
              textDecoration: "none",
              width: "fit-content",
            }}
          >
            {t("component.oauth_clients_panel.provider_docs")}<IconExternalLink size={13} />
          </a>
        )}
        <p style={{ fontSize: 12, color: "#78716c", margin: 0, lineHeight: 1.5 }}>
          {t("component.oauth_clients_panel.create_oauth_apps_in_the_provider_console_first_paypal")}<code style={codeStyle}>{client.client_id_env_var}</code> /
          <code style={codeStyle}>{client.client_secret_env_var}</code> {t("component.oauth_clients_panel.env_vars_for_this_provider_until_you_reset")}</p>
        <p style={{ fontSize: 11, color: "#a8a29e", margin: 0, lineHeight: 1.55 }}>
          {t("component.oauth_clients_panel.after_saving_go_to_integrations_connect_for_this_provi")}</p>
        <Field label={t("component.oauth_clients_panel.client_id")} required>
          <input
            type="text" autoFocus value={clientId} onChange={(e) => setClientId(e.target.value)}
            style={input}
          />
        </Field>
        <Field label={t("component.oauth_clients_panel.client_secret_2")} required hint={
          client.has_secret ? "Replacing the existing encrypted secret." : "First-time entry."
        }>
          <input
            type="password" placeholder={client.has_secret ? t("component.oauth_clients_panel.leave_blank_to_keep_current") : ""}
            value={clientSecret} onChange={(e) => setClientSecret(e.target.value)}
            style={input}
          />
        </Field>
        <Field label={t("component.oauth_clients_panel.scopes")} hint="Space-separated; leave blank to use the provider default.">
          <input
            type="text" value={scopes} onChange={(e) => setScopes(e.target.value)}
            placeholder={t("component.oauth_clients_panel.use_defaults")} style={input}
          />
        </Field>
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 4 }}>
          <Button variant="ghost" size="sm" onClick={onClose}>{t("action.cancel")}</Button>
          <Button
            variant="primary" size="sm"
            onClick={() => save.mutate()}
            disabled={!valid || save.isPending}
            loading={save.isPending}
          >
            {t("action.save")}</Button>
        </div>
      </div>
    </Modal>
  );
}

function Field({
  label, required, hint, children,
}: {
  label: string; required?: boolean; hint?: string;
  children: ReactNode;
}) {
  return (
    <div>
      <label style={{ display: "block", fontSize: 11, fontWeight: 700, color: "#57534e", marginBottom: 4, letterSpacing: "0.04em", textTransform: "uppercase" }}>
        {label}{required && <span style={{ color: "#c14a44", marginLeft: 2 }}>*</span>}
      </label>
      {children}
      {hint && <div style={{ fontSize: 11, color: "#a8a29e", marginTop: 4 }}>{hint}</div>}
    </div>
  );
}

const th: CSSProperties = {
  padding: "10px 14px", textAlign: "left", fontSize: 11, fontWeight: 700,
  color: "#57534e", textTransform: "uppercase", letterSpacing: "0.04em",
};
const td: CSSProperties = { padding: "12px 14px", verticalAlign: "top" };
const input: CSSProperties = {
  width: "100%", padding: "8px 10px", fontSize: 13,
  border: "1px solid rgba(28,25,23,0.06)", borderRadius: 8, fontFamily: "ui-monospace, monospace",
};
const codeStyle: CSSProperties = {
  background: "#f5f5f4", padding: "1px 5px", margin: "0 2px",
  borderRadius: 4, fontFamily: "ui-monospace, monospace", fontSize: 10,
};
