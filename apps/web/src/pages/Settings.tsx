import { useState, useEffect, type ComponentType } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useToastStore } from "../stores/toast";
import { useAuthStore } from "../stores/auth";
import Modal from "../components/ui/Modal";
import DeveloperTab from "../components/settings/DeveloperTab";
import {
  IconBell,
  IconBrain,
  IconCalendar,
  IconCode,
  IconDocument,
  IconPalette,
  IconSearch,
  IconShield,
  type IconProps,
} from "../components/icons";
import {
  AppearanceSection,
  AIModelSection,
  CalendarBookingSection,
  FilePermissionSection,
  SecuritySection as PasswordSection,
} from "./Account";
import { t } from "../lib/i18n";

/* ═══════════════════════════════════════════════════════════════════
   Shared components
   ═══════════════════════════════════════════════════════════════════ */

function NeonToggle({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <button type="button" onClick={() => onChange(!checked)} style={{
      position: "relative", width: 48, height: 26, borderRadius: 9999, border: "none", cursor: "pointer", flexShrink: 0, transition: "all 0.3s",
      background: checked ? "#4f7d75" : "#e7e5e4",
      boxShadow: checked ? "0 0 12px rgba(79,125,117,0.4)" : "inset 0 1px 2px rgba(0,0,0,0.06)",
    }}>
      <span style={{ position: "absolute", top: 3, left: checked ? 25 : 3, width: 20, height: 20, background: "#fff", borderRadius: "50%", boxShadow: "0 1px 3px rgba(0,0,0,0.12)", transition: "left 0.3s cubic-bezier(0.4,0,0.2,1)" }} />
    </button>
  );
}

const CHANNEL_LABELS: Record<string, string> = {
  inapp: "In-app",
  email: "Email",
  telegram: "Telegram",
  wechat: "WeChat",
  whatsapp: "WhatsApp",
  slack: "Slack",
  discord: "Discord",
  twilio_sms: "SMS",
};

function channelLabel(key: string): string {
  return CHANNEL_LABELS[key] || key;
}

function ChannelCheckbox({
  channel, checked, locked, onChange,
}: { channel: string; checked: boolean; locked?: boolean; onChange: (v: boolean) => void }) {
  return (
    <label style={{
      display: "inline-flex", alignItems: "center", gap: 6, padding: "4px 10px",
      borderRadius: 999, border: "1px solid",
      borderColor: checked ? "rgba(79,125,117,0.45)" : "rgba(214,211,209,0.7)",
      background: checked ? "rgba(79,125,117,0.08)" : "rgba(255,255,255,0.6)",
      color: locked ? "#a8a29e" : (checked ? "#436b65" : "#57534e"),
      fontSize: 12, fontWeight: 500, cursor: locked ? "not-allowed" : "pointer",
      userSelect: "none", whiteSpace: "nowrap",
    }}>
      <input
        type="checkbox"
        checked={checked}
        disabled={locked}
        onChange={(e) => !locked && onChange(e.target.checked)}
        style={{ accentColor: "#4f7d75", margin: 0 }}
      />
      {channelLabel(channel)}
    </label>
  );
}

function ConnectTelegramButton() {
  // Modal-style inline flow for "Connect Telegram":
  //   1. POST /preferences/link/start → token + deep link
  //   2. Show the user the deep link (or fallback command)
  //   3. Poll /preferences/link/<token> until it flips to "claimed"
  //   4. Invalidate the prefs query so Connected Channels refreshes
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const [open, setOpen] = useState(false);
  const [link, setLink] = useState<null | {
    token: string;
    deep_link: string | null;
    instructions: string;
    expires_at: string;
    bot_username: string | null;
  }>(null);
  const [status, setStatus] = useState<"pending" | "claimed" | "expired" | "not_found" | null>(null);

  const startMutation = useMutation({
    mutationFn: () => api.notifications.startChannelLink("telegram"),
    onSuccess: (data) => {
      setLink({
        token: data.token,
        deep_link: data.deep_link,
        instructions: data.instructions,
        expires_at: data.expires_at,
        bot_username: data.bot_username,
      });
      setStatus("pending");
      setOpen(true);
    },
    onError: (err: any) => {
      toast.error(t("page.settings.link_failed"), String(err?.message || err));
    },
  });

  // Poll the claim status every 3s while the modal is open.
  useEffect(() => {
    if (!open || !link || status !== "pending") return;
    let cancelled = false;
    const tick = async () => {
      try {
        const res = await api.notifications.getChannelLinkStatus(link.token);
        if (cancelled) return;
        setStatus(res.status);
        if (res.status === "claimed") {
          queryClient.invalidateQueries({ queryKey: ["notification-preferences"] });
          toast.success(t("page.settings.link_success"));
        }
      } catch {
        // best-effort polling; transient errors don't kill the modal
      }
    };
    const id = window.setInterval(tick, 3000);
    return () => { cancelled = true; window.clearInterval(id); };
  }, [open, link, status, queryClient, toast]);

  function close() {
    setOpen(false);
    setLink(null);
    setStatus(null);
  }

  return (
    <>
      <button
        type="button"
        className="btn-manor-secondary"
        onClick={() => startMutation.mutate()}
        disabled={startMutation.isPending}
        style={{ fontSize: 12, padding: "6px 12px" }}
      >
        {startMutation.isPending
          ? t("status.loading")
          : t("page.settings.connect_telegram")}
      </button>

      {open && link && (
        <Modal open={open} onClose={close} title={t("page.settings.connect_telegram")}>
          <div style={{ display: "flex", flexDirection: "column", gap: 12, padding: 4 }}>
            {status === "claimed" ? (
              <div style={{
                padding: 14, borderRadius: 10,
                background: "rgba(79,156,132,0.08)",
                border: "1px solid rgba(79,156,132,0.35)",
                color: "#3f7361", fontSize: 13,
              }}>
                {t("page.settings.link_success")}
              </div>
            ) : status === "expired" ? (
              <div style={{
                padding: 14, borderRadius: 10,
                background: "rgba(209,139,134,0.08)",
                border: "1px solid rgba(209,139,134,0.35)",
                color: "#a23e38", fontSize: 13,
              }}>
                {t("page.settings.link_expired")}
              </div>
            ) : (
              <>
                <div style={{ fontSize: 13, color: "#57534e" }}>
                  {link.instructions}
                </div>
                {link.deep_link && (
                  <a
                    href={link.deep_link}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="btn-manor"
                    style={{ textAlign: "center" }}
                  >
                    {t("page.settings.open_telegram")}
                  </a>
                )}
                <div style={{
                  padding: 10, borderRadius: 8,
                  background: "rgba(28,25,23,0.04)",
                  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                  fontSize: 13, color: "#1c1917", wordBreak: "break-all",
                }}>
                  /start {link.token}
                </div>
                <div style={{ fontSize: 11, color: "#a8a29e" }}>
                  {t("page.settings.link_polling")}
                </div>
              </>
            )}
            <button type="button" className="btn-manor-secondary" onClick={close}>
              {status === "claimed" ? t("common.done") : t("common.cancel")}
            </button>
          </div>
        </Modal>
      )}
    </>
  );
}


function NotificationsTab() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const { data, isLoading } = useQuery({
    queryKey: ["notification-preferences"],
    queryFn: () => api.notifications.getPreferences(),
  });

  const updateMutation = useMutation({
    mutationFn: (patch: any) => api.notifications.updatePreferences(patch),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["notification-preferences"] });
      toast.success(t("page.settings.settings_saved"));
    },
  });

  if (isLoading || !data) {
    return <div style={{ color: "#a8a29e" }}>{t("status.loading")}</div>;
  }

  const supported = data.supported_channels || [];
  const catalog = data.event_catalog || [];
  const connected = data.connected_channels || [];
  const configuredChannels = data.configured_channels || [];
  const connectedTypes = new Set(connected.map((c) => c.channel_type));
  const configuredTypes = new Set(configuredChannels);
  const visibleChannels = [
    "inapp",
    ...supported.filter((ct) => ct !== "inapp" && connectedTypes.has(ct)),
  ];
  const linkableChannels = supported.filter(
    (ct) => ct !== "inapp" && configuredTypes.has(ct) && !connectedTypes.has(ct),
  );
  const canLinkTelegram = linkableChannels.includes("telegram");
  const configuredLabels = configuredChannels.length
    ? configuredChannels.map(channelLabel).join(", ")
    : t("page.settings.none_configured");
  const defaults = new Set(data.default_channels || []);
  const byKind = data.by_kind || {};
  const quiet = data.quiet_hours || null;

  const effectiveChannelsForKind = (kind: string): Set<string> => {
    const usable = new Set(visibleChannels);
    const override = byKind[kind];
    if (override?.enabled === false) return new Set(["inapp"]);
    if (override?.channels && override.channels.length > 0) {
      return new Set(["inapp", ...override.channels.filter((ct) => usable.has(ct))]);
    }
    return new Set(["inapp", ...Array.from(defaults).filter((ct) => usable.has(ct))]);
  };

  const toggleDefault = (channel: string, checked: boolean) => {
    const next = new Set(defaults);
    if (checked) next.add(channel); else next.delete(channel);
    next.delete("inapp");                     // inapp is always-on; not stored
    updateMutation.mutate({ default_channels: Array.from(next) });
  };

  const toggleKindChannel = (kind: string, channel: string, checked: boolean) => {
    const current = effectiveChannelsForKind(kind);
    const next = new Set(current);
    if (checked) next.add(channel); else next.delete(channel);
    next.delete("inapp");
    const override = { channels: Array.from(next), enabled: true };
    updateMutation.mutate({ by_kind: { [kind]: override } });
  };

  const resetKind = (kind: string) => {
    updateMutation.mutate({ by_kind: { [kind]: null } });
  };

  const toggleKindEnabled = (kind: string, enabled: boolean) => {
    if (enabled) {
      updateMutation.mutate({ by_kind: { [kind]: null } });
    } else {
      updateMutation.mutate({ by_kind: { [kind]: { enabled: false, channels: [] } } });
    }
  };

  const updateQuietHours = (patch: Partial<{ tz: string; from: string; to: string }>) => {
    if (!quiet) {
      updateMutation.mutate({ quiet_hours: { tz: "UTC", from: "22:00", to: "08:00", ...patch } });
      return;
    }
    updateMutation.mutate({ quiet_hours: { ...quiet, ...patch } });
  };

  const clearQuietHours = () => updateMutation.mutate({ quiet_hours: { from: null, to: null } as any });

  // Group catalog by category for nicer presentation.
  const grouped = catalog.reduce<Record<string, typeof catalog>>((acc, entry) => {
    (acc[entry.category] ||= []).push(entry);
    return acc;
  }, {});
  const categoryOrder = ["task", "calendar", "agent", "media", "system", "billing"];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 28 }}>
      <section
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))",
          gap: 10,
        }}
      >
        <div className="glass-card" style={{ padding: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: "#292524", marginBottom: 4 }}>
            {t("page.settings.notification_routing_controls")}
          </div>
          <div style={{ fontSize: 12, color: "#78716c", lineHeight: 1.5 }}>
            {t("page.settings.notification_routing_controls_desc")}
          </div>
        </div>
        <div className="glass-card" style={{ padding: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: "#292524", marginBottom: 4 }}>
            {t("page.settings.configured_notification_integrations")}
          </div>
          <div style={{ fontSize: 12, color: "#78716c", lineHeight: 1.5 }}>
            {t("page.settings.configured_notification_integrations_desc")}
          </div>
          <div style={{ marginTop: 8, display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <span style={{
              display: "inline-flex", alignItems: "center", padding: "4px 9px",
              borderRadius: 999, border: "1px solid rgba(28,25,23,0.08)",
              color: "#57534e", fontSize: 11, background: "rgba(255,255,255,0.68)",
            }}>
              {configuredLabels}
            </span>
            <button
              type="button"
              onClick={() => navigate("/integrations")}
              style={{
                fontSize: 11, padding: "4px 9px", borderRadius: 999,
                border: "1px solid rgba(79,125,117,0.28)",
                background: "rgba(79,125,117,0.08)", color: "#436b65",
                cursor: "pointer", fontWeight: 700,
              }}
            >
              {t("page.settings.open_integrations")}
            </button>
          </div>
        </div>
      </section>

      {/* Connected channels */}
      <section>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 4 }}>
          <h3 className="manor-section-title" style={{ margin: 0 }}>
            {t("page.settings.connected_channels")}
          </h3>
          {connected.length > 0 && canLinkTelegram && <ConnectTelegramButton />}
        </div>
        <p style={{ margin: "0 0 12px", fontSize: 12, color: "#78716c" }}>
          {t("page.settings.connected_channels_desc")}
        </p>
        {connected.length === 0 ? (
          <div style={{
            padding: 16, border: "1px dashed rgba(28,25,23,0.06)", borderRadius: 12,
            background: "rgba(250,250,249,0.5)", color: "#78716c", fontSize: 13,
          }}>
            {t("page.settings.no_channels_connected")}
            {canLinkTelegram && (
              <div style={{ marginTop: 10 }}>
                <ConnectTelegramButton />
              </div>
            )}
          </div>
        ) : (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {connected.map((c) => (
              <span key={c.contact_id} style={{
                display: "inline-flex", alignItems: "center", gap: 8, padding: "6px 12px",
                borderRadius: 999, background: "rgba(79,125,117,0.08)",
                border: "1px solid rgba(79,125,117,0.25)", fontSize: 12,
              }}>
                <strong style={{ color: "#436b65" }}>{channelLabel(c.channel_type)}</strong>
                <span style={{ color: "#57534e" }}>{c.display_name || c.source_id}</span>
              </span>
            ))}
          </div>
        )}
      </section>

      {/* Default channels */}
      <section>
        <h3 className="manor-section-title">{t("page.settings.default_routes")}</h3>
        <p style={{ margin: "0 0 12px", fontSize: 12, color: "#78716c" }}>
          {t("page.settings.default_routes_desc")}
        </p>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          {visibleChannels.map((ct) => (
            <ChannelCheckbox
              key={ct}
              channel={ct}
              checked={ct === "inapp" || defaults.has(ct)}
              locked={ct === "inapp"}
              onChange={(v) => toggleDefault(ct, v)}
            />
          ))}
        </div>
      </section>

      {/* Per-event matrix */}
      <section>
        <h3 className="manor-section-title">{t("page.settings.event_routing")}</h3>
        <p style={{ margin: "0 0 12px", fontSize: 12, color: "#78716c" }}>
          {t("page.settings.event_routing_desc")}
        </p>
        {categoryOrder.filter((c) => grouped[c]).map((category) => (
          <div key={category} style={{ marginBottom: 16 }}>
            <div style={{
              fontSize: 11, textTransform: "uppercase", letterSpacing: 0.6,
              color: "#a8a29e", margin: "0 0 8px", fontWeight: 600,
            }}>
              {category}
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {grouped[category].map((entry) => {
                const override = byKind[entry.kind];
                const enabled = override?.enabled !== false;
                const eff = effectiveChannelsForKind(entry.kind);
                const customized = !!override;
                return (
                  <div key={entry.kind} className="glass-card" style={{ padding: 14 }}>
                    <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12, marginBottom: 8 }}>
                      <div style={{ minWidth: 0 }}>
                        <div style={{ fontSize: 14, fontWeight: 600, color: "#292524" }}>{entry.label}</div>
                        <div style={{ fontSize: 12, color: "#78716c", marginTop: 2 }}>{entry.description}</div>
                      </div>
                      <div style={{ display: "flex", alignItems: "center", gap: 8, flexShrink: 0 }}>
                        {customized && (
                          <button
                            type="button"
                            onClick={() => resetKind(entry.kind)}
                            style={{
                              fontSize: 11, padding: "4px 8px", borderRadius: 6,
                              border: "1px solid rgba(28,25,23,0.06)",
                              background: "rgba(255,255,255,0.7)", color: "#78716c",
                              cursor: "pointer",
                            }}
                          >
                            {t("page.settings.use_default")}
                          </button>
                        )}
                        <NeonToggle checked={enabled} onChange={(v) => toggleKindEnabled(entry.kind, v)} />
                      </div>
                    </div>
                    {enabled && (
                      <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                        {visibleChannels.map((ct) => (
                          <ChannelCheckbox
                            key={ct}
                            channel={ct}
                            checked={eff.has(ct)}
                            locked={ct === "inapp"}
                            onChange={(v) => toggleKindChannel(entry.kind, ct, v)}
                          />
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </section>

      {/* Quiet hours */}
      <section>
        <h3 className="manor-section-title">{t("page.settings.quiet_hours")}</h3>
        <p style={{ margin: "0 0 12px", fontSize: 12, color: "#78716c" }}>
          {t("page.settings.quiet_hours_desc")}
        </p>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "center" }}>
          <input
            className="manor-input"
            type="time"
            value={quiet?.from || ""}
            onChange={(e) => updateQuietHours({ from: e.target.value })}
            style={{ maxWidth: 130 }}
          />
          <span style={{ color: "#78716c" }}>—</span>
          <input
            className="manor-input"
            type="time"
            value={quiet?.to || ""}
            onChange={(e) => updateQuietHours({ to: e.target.value })}
            style={{ maxWidth: 130 }}
          />
          <input
            className="manor-input"
            placeholder="UTC"
            value={quiet?.tz || ""}
            onChange={(e) => updateQuietHours({ tz: e.target.value })}
            style={{ maxWidth: 180 }}
          />
          {quiet && (
            <button
              type="button"
              onClick={clearQuietHours}
              style={{
                fontSize: 12, padding: "6px 12px", borderRadius: 8,
                border: "1px solid rgba(28,25,23,0.06)",
                background: "rgba(255,255,255,0.7)", color: "#78716c",
                cursor: "pointer",
              }}
            >
              {t("common.clear")}
            </button>
          )}
        </div>
      </section>
    </div>
  );
}


/* ═══════════════════════════════════════════════════════════════════
   TAB 4 — Security (2FA)
   ═══════════════════════════════════════════════════════════════════ */

function SecurityTab() {
  const [step, setStep] = useState<"idle" | "verify" | "disable">("idle");
  const [code, setCode] = useState("");
  const [setupData, setSetupData] = useState<{ secret: string; uri: string } | null>(null);
  const [backupCodes, setBackupCodes] = useState<string[] | null>(null);
  const [error, setError] = useState("");
  const { data: tfaStatus, isLoading, refetch } = useQuery({ queryKey: ["2fa-status"], queryFn: () => api.twoFactor.status() });
  const enabled = tfaStatus?.enabled ?? (tfaStatus as any)?.totp_enabled ?? false;

  const handleSetup = async () => { setError(""); try { const d = await api.twoFactor.setup(); setSetupData(d); setStep("verify"); } catch (e: any) { setError(e.message); } };
  const handleVerify = async () => { setError(""); try { const r = await api.twoFactor.verify(code.trim()); if (r.backup_codes) setBackupCodes(r.backup_codes); setStep("idle"); setCode(""); setSetupData(null); refetch(); } catch (e: any) { setError(e.message); } };
  const handleDisable = async () => { setError(""); try { await api.twoFactor.disable(code.trim()); setStep("idle"); setCode(""); setBackupCodes(null); refetch(); } catch (e: any) { setError(e.message); } };

  if (isLoading) return null;
  return (
    <div className="glass-card" style={{ padding: "24px 28px" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div>
          <h4 className="manor-section-title" style={{ margin: 0 }}>{t("page.settings.two_factor_auth")}</h4>
          <p style={{ fontSize: 13, marginTop: 4, marginBottom: 0, color: enabled ? "#437f6b" : "#a8a29e" }}>{enabled ? t("page.settings.two_factor_enabled") : t("page.settings.two_factor_disabled")}</p>
        </div>
        {step === "idle" && !enabled && <button className="btn-manor" onClick={handleSetup}>{t("page.settings.enable_2fa")}</button>}
        {step === "idle" && enabled && <button className="btn-manor-danger" onClick={() => { setStep("disable"); setCode(""); setError(""); }}>{t("page.settings.disable_2fa")}</button>}
      </div>
      {step === "verify" && setupData && (
        <div style={{ borderTop: "1px solid rgba(28,25,23,0.06)", paddingTop: 16, marginTop: 16 }}>
          <label className="manor-label">{t("page.settings.scan_this_qr_code_with_your_authenticator_app")}</label>
          <div style={{ display: "flex", justifyContent: "center", margin: "12px 0 16px" }}>
            <img
              src={`https://api.qrserver.com/v1/create-qr-code/?size=180x180&data=${encodeURIComponent(setupData.uri)}`}
              alt={t("page.settings.2fa_qr_code")}
              style={{ width: 180, height: 180, borderRadius: 12, border: "1px solid rgba(28,25,23,0.06)" }}
            />
          </div>
          <label className="manor-label">{t("page.settings.or_enter_secret_manually")}</label>
          <code style={{ display: "block", fontSize: 13, background: "#fafaf9", padding: 10, borderRadius: 10, userSelect: "all", fontFamily: "monospace", marginBottom: 12, letterSpacing: "0.05em", fontWeight: 700 }}>{setupData.secret}</code>
          <label className="manor-label">{t("page.settings.6_digit_code")}</label>
          <input className="manor-input" value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))} placeholder="000000" maxLength={6} style={{ width: 160, fontFamily: "monospace", letterSpacing: "0.1em" }} />
          {error && <p style={{ fontSize: 13, color: "#c14a44", margin: "8px 0" }}>{error}</p>}
          <div style={{ display: "flex", gap: 8, marginTop: 12 }}><button className="btn-manor" onClick={handleVerify} disabled={code.length < 6}>{t("page.settings.verify_enable")}</button><button className="btn-manor-ghost" onClick={() => { setStep("idle"); setCode(""); setSetupData(null); setError(""); }}>{t("action.cancel")}</button></div>
        </div>
      )}
      {step === "disable" && (
        <div style={{ borderTop: "1px solid rgba(28,25,23,0.06)", paddingTop: 16, marginTop: 16 }}>
          <label className="manor-label">{t("page.settings.enter_current_totp_code_to_disable")}</label>
          <input className="manor-input" value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))} placeholder="000000" maxLength={6} style={{ width: 160, fontFamily: "monospace", letterSpacing: "0.1em" }} />
          {error && <p style={{ fontSize: 13, color: "#c14a44", margin: "8px 0" }}>{error}</p>}
          <div style={{ display: "flex", gap: 8, marginTop: 12 }}><button className="btn-manor-danger" onClick={handleDisable} disabled={code.length < 6}>{t("page.settings.confirm_disable")}</button><button className="btn-manor-ghost" onClick={() => { setStep("idle"); setCode(""); setError(""); }}>{t("action.cancel")}</button></div>
        </div>
      )}
      {backupCodes && (
        <div style={{ borderTop: "1px solid rgba(28,25,23,0.06)", paddingTop: 16, marginTop: 16 }}>
          <p style={{ fontSize: 13, fontWeight: 600, color: "#c14a44", marginBottom: 8 }}>{t("page.settings.save_backup_codes")}</p>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))", gap: 4, background: "#f3ecd6", padding: 12, borderRadius: 10, border: "1px solid #ddbb63" }}>
            {backupCodes.map((bc, i) => <code key={i} style={{ fontSize: 12, fontFamily: "monospace", color: "#78350f" }}>{bc}</code>)}
          </div>
          <button className="btn-manor-teal-light" onClick={() => setBackupCodes(null)} style={{ marginTop: 10, fontSize: 12 }}>{t("page.settings.saved_backup_codes")}</button>
        </div>
      )}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   Main Settings Page
   ═══════════════════════════════════════════════════════════════════ */

type SettingsTabKey =
  | "appearance"
  | "ai"
  | "calendar"
  | "files"
  | "notifications"
  | "security"
  | "developer";

type SettingsNavItem = {
  key: SettingsTabKey;
  label: string;
  description: string;
  icon: ComponentType<IconProps>;
};

type SettingsNavGroup = {
  label: string;
  items: SettingsNavItem[];
};

const PERSONAL_SETTINGS: SettingsNavItem[] = [
  {
    key: "appearance",
    label: t("page.account.appearance"),
    description: "Theme, display mode, and interface preference.",
    icon: IconPalette,
  },
  {
    key: "ai",
    label: "AI models",
    description: "Choose models for reasoning, worker jobs, and media generation.",
    icon: IconBrain,
  },
  {
    key: "calendar",
    label: "Calendar",
    description: "Connect booking links and availability for scheduled work.",
    icon: IconCalendar,
  },
  {
    key: "files",
    label: "File permissions",
    description: "Control how agents can read, create, and update files.",
    icon: IconDocument,
  },
  {
    key: "notifications",
    label: t("nav.notifications"),
    description: "Configure alerts, quiet hours, and notification channels.",
    icon: IconBell,
  },
  {
    key: "security",
    label: t("page.account.security"),
    description: "Manage password settings and two-factor authentication.",
    icon: IconShield,
  },
];


const DEVELOPER_SETTINGS: SettingsNavItem = {
  key: "developer",
  label: t("page.settings.developer"),
  description: "Developer configuration for advanced integrations.",
  icon: IconCode,
};

// Temporarily hidden pending a redesign. The old Developer tab only
// exposed deployment-level OAuth client credentials, which now live in
// the platform admin portal (/admin → Integrations). Flip back to true
// once the redesigned tenant-facing Developer surface ships.
const SHOW_DEVELOPER_TAB = false;

export default function Settings() {
  const user = useAuthStore((s) => s.user);
  const isAdmin = user?.role === "admin" || user?.role === "owner";
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [settingsSearch, setSettingsSearch] = useState("");


  const settingsGroups: SettingsNavGroup[] = [
    { label: "Personal", items: PERSONAL_SETTINGS },
    ...(isAdmin && SHOW_DEVELOPER_TAB
      ? [{ label: "Advanced", items: [DEVELOPER_SETTINGS] }]
      : []),
  ];
  const settingsItems = settingsGroups.flatMap((group) => group.items);

  const defaultTab: SettingsTabKey =
    "appearance";
  const initialTab = (searchParams.get("tab") || defaultTab) as SettingsTabKey;
  const [tab, setTab] = useState<SettingsTabKey>(
    settingsItems.some((item) => item.key === initialTab) ? initialTab : defaultTab,
  );
  useEffect(() => {
    setTab(settingsItems.some((item) => item.key === initialTab) ? initialTab : defaultTab);
  }, [initialTab, isAdmin, defaultTab]);

  // Notification preferences are owned by the NotificationsTab itself —
  // it queries /api/v1/notifications/preferences directly, so the parent
  // doesn't need to prefetch anything here.

  const activeItem = settingsItems.find((item) => item.key === tab) || settingsItems[0];
  const activeGroup = settingsGroups.find((group) => group.items.some((item) => item.key === activeItem.key));
  const normalizedSearch = settingsSearch.trim().toLowerCase();
  const visibleGroups = settingsGroups
    .map((group) => ({
      ...group,
      items: group.items.filter((item) => {
        if (!normalizedSearch) return true;
        return `${item.label} ${item.description} ${group.label}`.toLowerCase().includes(normalizedSearch);
      }),
    }))
    .filter((group) => group.items.length > 0);

  const handleTabChange = (nextTab: SettingsTabKey) => {
    setTab(nextTab);
    navigate(`/settings?tab=${encodeURIComponent(nextTab)}`, { replace: true });
  };

  const content = (() => {
    if (tab === "appearance") return <AppearanceSection embedded />;
    if (tab === "notifications") return <NotificationsTab />;
    if (tab === "calendar" && user) return <CalendarBookingSection user={user} />;
    if (tab === "ai" && user) return <AIModelSection user={user} settingsSurface />;
    if (tab === "files") return <FilePermissionSection />;
    if (tab === "security") {
      return (
        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <PasswordSection />
          <SecurityTab />
        </div>
      );
    }
    if (tab === "developer" && isAdmin && SHOW_DEVELOPER_TAB) return <DeveloperTab />;
    return null;
  })();

  return (
    <div
      className="settings-page"
      style={{ height: "100%", overflowY: "auto", padding: "clamp(16px, 4vw, 26px)", animation: "fade-in 0.3s ease-out" }}
    >
      <div className="settings-shell">
        <aside className="settings-local-sidebar" aria-label="Settings navigation">
          <button
            type="button"
            className="settings-back-to-app"
            onClick={() => navigate("/dashboard")}
          >
            <span aria-hidden="true">{"<"}</span>
            Back to app
          </button>

          <div className="settings-sidebar-head">
            <h1>{t("nav.settings")}</h1>
          </div>

          <label className="settings-sidebar-search">
            <IconSearch size={15} />
            <input
              value={settingsSearch}
              onChange={(event) => setSettingsSearch(event.target.value)}
              placeholder="Search settings..."
            />
          </label>

          <div className="settings-sidebar-groups">
            {visibleGroups.map((group) => (
              <div className="settings-sidebar-group" key={group.label}>
                <p className="settings-sidebar-group-label">{group.label}</p>
                <div className="settings-sidebar-items">
                  {group.items.map((item) => {
                    const Icon = item.icon;
                    return (
                      <button
                        type="button"
                        key={item.key}
                        className={`settings-sidebar-item${item.key === tab ? " is-active" : ""}`}
                        onClick={() => handleTabChange(item.key)}
                      >
                        <span className="settings-sidebar-item-icon"><Icon size={16} /></span>
                        <span className="settings-sidebar-item-copy">
                          <span>{item.label}</span>
                        </span>
                      </button>
                    );
                  })}
                </div>
              </div>
            ))}
            {visibleGroups.length === 0 && (
              <div className="settings-sidebar-empty">No settings found</div>
            )}
          </div>
        </aside>

        <main className="settings-detail">
          <div className="settings-detail-header">
            <p className="settings-detail-kicker">{activeGroup?.label || "Settings"}</p>
            <h2>{activeItem.label}</h2>
            <p>{activeItem.description}</p>
          </div>

          <div className="settings-content-panel">
            {content}
          </div>
        </main>
      </div>
    </div>
  );
}
