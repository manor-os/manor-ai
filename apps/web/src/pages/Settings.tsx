import { startVisiblePolling } from "../lib/visiblePolling";
import { displayReadInterval } from "../lib/readRefreshPolicy";
import {
  lazy, Suspense, useState, useEffect, type ComponentType, type ReactNode,
} from "react";
import { Navigate, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  api,
  type OAuthConnection,
  type PersonalAccessTokenCreated,
} from "../lib/api";
import { useToastStore } from "../stores/toast";
import { useAuthStore } from "../stores/auth";
import { useDisplayPreferences } from "../stores/displayPreferences";
import Modal from "../components/ui/Modal";
import Button from "../components/ui/Button";
import Select from "../components/ui/Select";
import Input from "../components/ui/Input";
import Checkbox from "../components/ui/Checkbox";
import Toggle from "../components/ui/Toggle";
import { ConfigBlock, ConfigGroup, ConfigRow } from "../components/ui/GroupedList";
import Card from "../components/ui/Card";
import EmptyState from "../components/ui/EmptyState";
import Chip from "../components/ui/Chip";
import ConfirmDialog from "../components/ui/ConfirmDialog";
import PageHeader, { PageHeaderBoundary } from "../components/ui/PageHeader";
import { ListRowsSkeleton, PanelLoading } from "../components/ui/Skeleton";
import DeveloperTab from "../components/settings/DeveloperTab";
import {
  IconArrowLeft,
  IconBell,
  IconAgent, IconBookmark, IconChat, IconClock,
  IconBuilding,
  IconConnection,
  IconPeople,
  IconBrain,
  IconFlow,
  IconSkill,
  IconCalendar,
  IconCode,
  IconPalette,
  IconSearch,
  IconShield,
  IconCheckCircle,
  type IconProps, IconCloud } from "../components/icons";
import {
  AppearanceSection,
  AIModelSection,
  CalendarBookingSection,
  FilePermissionSection,
  SecuritySection as PasswordSection,
} from "./Account";
// The AI libraries load only when their tab opens.
const Agents = lazy(() => import("./Agents"));
const Skills = lazy(() => import("./Skills"));
const Memories = lazy(() => import("./Memories"));
const ScheduledJobs = lazy(() => import("./ScheduledJobs"));
const ChatHistory = lazy(() => import("./ChatHistory"));
const Flows = lazy(() => import("./Flows"));
import { usePreviewFeatureAccess } from "../lib/previewFeatureAccess";
import { CLIENTS_PATH } from "../lib/peopleNavigation";
import { FEATURE_DISCOVERIES } from "../lib/featureDiscovery";
import useFeatureDiscovery from "../components/useFeatureDiscovery";
import { FeatureDiscoveryBadge } from "../components/ui/FeatureDiscoveryHint";
import { t } from "../lib/i18n";
import { localizeCatalogText } from "../lib/eventCopy";
import {
  formatDateOnly, relativeTime,
} from "../lib/format";


/* ═══════════════════════════════════════════════════════════════════
   {t("ui.shared_components")}
   ═══════════════════════════════════════════════════════════════════ */

const CHANNEL_LABELS: Record<string, string> = {
  inapp: t("ui.channel_in_app"),
  email: t("ui.channel_email"),
  telegram: "Telegram",
  wechat: "WeChat",
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
      color: locked ? "#aeaeb2" : (checked ? "#436b65" : "#6e6e73"),
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

type ClaimableChannel = "telegram" | "discord";

function ConnectChannelButton({ channelType }: { channelType: ClaimableChannel }) {
  // Modal-style inline flow for explicitly binding a channel identity:
  //   1. POST /preferences/link/start → token + deep link
  //   2. Show the user the provider deep link (or fallback command)
  //   3. Poll /preferences/link/<token> until it flips to "claimed"
  //   4. Invalidate the prefs query so Connected Channels refreshes
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const label = channelLabel(channelType);
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
    mutationFn: () => api.notifications.startChannelLink(channelType),
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
    const poll = startVisiblePolling(async (signal) => {
      if (Date.now() >= Date.parse(link.expires_at)) { setStatus("expired"); return; }
      const res = await api.notifications.getChannelLinkStatus(link.token, signal);
      if (signal.aborted) return;
      setStatus(res.status);
      if (res.status === "claimed") {
        void queryClient.invalidateQueries({ queryKey: ["notification-preferences"] });
        toast.success(t("page.settings.link_success"));
      }
    }, 3_000);
    return poll.stop;
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
          : t("page.settings.connect_channel", { channel: label })}
      </button>

      {open && link && (
        <Modal open={open} onClose={close} title={t("page.settings.connect_channel", { channel: label })}>
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
                <div style={{ fontSize: 13, color: "var(--text-muted)" }}>
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
                    {t("page.settings.open_channel", { channel: label })}
                  </a>
                )}
                <div style={{
                  padding: 10, borderRadius: 8,
                  background: "rgba(29, 29, 31,0.04)",
                  fontFamily: "var(--font-mono)",
                  fontSize: 13, color: "var(--text-strong)", wordBreak: "break-all",
                }}>
                  /start {link.token}
                </div>
                <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
                  {t("page.settings.link_polling_channel", { channel: label })}
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
    onError: (error: Error) => toast.error(t("page.settings.settings_save_failed"), error.message),
  });

  if (isLoading || !data) {
    return <PanelLoading rows={4} minHeight={240} />;
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
  const claimableChannels = linkableChannels.filter(
    (channel): channel is ClaimableChannel => channel === "telegram" || channel === "discord",
  );
  const configuredLabels = configuredChannels.length
    ? configuredChannels.map(channelLabel).join(", ")
    : t("page.settings.none_configured");
  const defaults = new Set(data.default_channels || []);
  const byKind = data.by_kind || {};
  const quiet = data.quiet_hours || null;
  const chatEmail = data.chat_email ?? { enabled: true, delay_minutes: 30 };

  const effectiveChannelsForKind = (kind: string): Set<string> => {
    const usable = new Set(visibleChannels);
    const override = byKind[kind];
    if (override?.enabled === false) return new Set(["inapp"]);
    if (override?.channels && override.channels.length > 0) {
      return new Set(["inapp", ...override.channels.filter((ct) => usable.has(ct))]);
    }
    const kindDefaults = catalog.find((entry) => entry.kind === kind)?.default_channels;
    const base = kindDefaults && kindDefaults.length > 0 ? kindDefaults : Array.from(defaults);
    return new Set(["inapp", ...base.filter((ct) => usable.has(ct))]);
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
    // An empty list reads as "no override" server-side and falls back to the
    // kind's default channels. Keep ["inapp"] so unchecking the last external
    // channel really stops it.
    const external = Array.from(next);
    const override = { channels: external.length > 0 ? external : ["inapp"], enabled: true };
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
  const categoryOrder = ["task", "calendar", "agent", "media", "system", "billing", "marketplace"];

  return (
    <div className="config-page">
      <ConfigGroup title={t("page.settings.chat_email_title")} id="chat-email-heading">
        <ConfigRow
          label={t("page.settings.chat_email_title")}
          note={t("page.settings.chat_email_description")}
          control={(
            <Toggle
              checked={chatEmail.enabled}
              disabled={updateMutation.isPending}
              aria-label={t("page.settings.chat_email_title")}
              onChange={() => updateMutation.mutate({ chat_email: { enabled: !chatEmail.enabled } })}
            />
          )}
        />
        <ConfigRow
          label={t("page.settings.chat_email_delay")}
          control={(
            <Select
              id="chat-email-delay"
              ariaLabel={t("page.settings.chat_email_delay")}
              value={String(chatEmail.delay_minutes)}
              disabled={!chatEmail.enabled || updateMutation.isPending}
              onChange={(value) => updateMutation.mutate({ chat_email: { delay_minutes: Number(value) } })}
              options={[5, 15, 30, 60, 120].map((minutes) => ({ value: String(minutes), label: t("page.settings.chat_email_minutes", { count: minutes }) }))}
            />
          )}
        />
      </ConfigGroup>


      <ConfigGroup title={t("page.settings.connected_channels")}>
        {connected.length === 0 && (
          <ConfigBlock><p className="config-empty">{t("page.settings.no_channels_connected")}</p></ConfigBlock>
        )}
        {connected.map((c) => (
          <ConfigRow
            key={c.contact_id}
            label={channelLabel(c.channel_type)}
            meta={localizeCatalogText(c.display_name || c.source_id)}
          />
        ))}
        {claimableChannels.length > 0 && (
          <ConfigBlock>
            <div className="config-chips">
              {claimableChannels.map((channel) => (
                <ConnectChannelButton key={channel} channelType={channel} />
              ))}
            </div>
          </ConfigBlock>
        )}
        <ConfigRow
          label={t("page.settings.configured_notification_integrations")}
          meta={configuredLabels}
          chevron
          onClick={() => navigate("/integrations")}
        />
      </ConfigGroup>

      <ConfigGroup title={t("page.settings.default_routes")}>
        <ConfigBlock>
          <div className="config-chips">
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
        </ConfigBlock>
      </ConfigGroup>

      {categoryOrder.filter((c) => grouped[c]).map((category) => (
        <ConfigGroup key={category} title={t(`page.settings.notification_category_${category}`)}>
          {grouped[category].map((entry) => {
            const override = byKind[entry.kind];
            const enabled = override?.enabled !== false;
            const eff = effectiveChannelsForKind(entry.kind);
            return (
              <div key={entry.kind}>
                <ConfigRow
                  label={localizeCatalogText(entry.label)}
                  note={localizeCatalogText(entry.description)}
                  control={(
                    <span className="config-row-actions">
                      {override && (
                        <Button variant="ghost" size="sm" onClick={() => resetKind(entry.kind)}>
                          {t("page.settings.use_default")}
                        </Button>
                      )}
                      <Toggle
                        checked={enabled}
                        aria-label={localizeCatalogText(entry.label)}
                        onChange={() => toggleKindEnabled(entry.kind, !enabled)}
                      />
                    </span>
                  )}
                />
                {enabled && (
                  <div className="config-row-chips">
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
        </ConfigGroup>
      ))}

      <ConfigGroup
        title={t("page.settings.quiet_hours")}
        action={quiet ? <button type="button" className="config-group-link" onClick={clearQuietHours}>{t("common.clear")}</button> : undefined}
      >
        <ConfigRow
          label={t("page.settings.quiet_hours_from")}
          htmlFor="quiet-from"
          control={<input id="quiet-from" className="config-input config-input--short" type="time" value={quiet?.from || ""} onChange={(e) => updateQuietHours({ from: e.target.value })} />}
        />
        <ConfigRow
          label={t("page.settings.quiet_hours_to")}
          htmlFor="quiet-to"
          control={<input id="quiet-to" className="config-input config-input--short" type="time" value={quiet?.to || ""} onChange={(e) => updateQuietHours({ to: e.target.value })} />}
        />
        <ConfigRow
          label={t("page.account.timezone")}
          htmlFor="quiet-tz"
          control={<input id="quiet-tz" className="config-input config-input--short" placeholder="UTC" value={quiet?.tz || ""} onChange={(e) => updateQuietHours({ tz: e.target.value })} />}
        />
      </ConfigGroup>
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
  const handleVerify = async () => {
    setError("");
    try {
      const result = await api.twoFactor.verify(code.trim());
      if (result.access_token) {
        localStorage.setItem("manor_token", result.access_token);
        useAuthStore.setState({ token: result.access_token });
      }
      if (result.backup_codes) setBackupCodes(result.backup_codes);
      setStep("idle");
      setCode("");
      setSetupData(null);
      await refetch();
    } catch (e: any) {
      setError(e.message);
    }
  };
  const handleDisable = async () => { setError(""); try { await api.twoFactor.disable(code.trim()); setStep("idle"); setCode(""); setBackupCodes(null); refetch(); } catch (e: any) { setError(e.message); } };

  if (isLoading) {
    return (
      <div className="glass-card" style={{ padding: "24px 28px" }}>
        <ListRowsSkeleton rows={2} action />
      </div>
    );
  }
  return (
    <div className="glass-card" style={{ padding: "24px 28px" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div>
          <h4 className="manor-section-title" style={{ margin: 0 }}>{t("page.settings.two_factor_auth")}</h4>
          <p style={{ fontSize: 13, marginTop: 4, marginBottom: 0, color: enabled ? "#437f6b" : "#aeaeb2" }}>{enabled ? t("page.settings.two_factor_enabled") : t("page.settings.two_factor_disabled")}</p>
        </div>
        {step === "idle" && !enabled && <button className="btn-manor-outline" onClick={handleSetup}>{t("page.settings.enable_2fa")}</button>}
        {step === "idle" && enabled && <button className="btn-manor-danger" onClick={() => { setStep("disable"); setCode(""); setError(""); }}>{t("page.settings.disable_2fa")}</button>}
      </div>
      {step === "verify" && setupData && (
        <div style={{ borderTop: "0.5px solid rgba(29, 29, 31,0.06)", paddingTop: 16, marginTop: 16 }}>
          <label className="manor-label">{t("page.settings.scan_this_qr_code_with_your_authenticator_app")}</label>
          <div style={{ display: "flex", justifyContent: "center", margin: "12px 0 16px" }}>
            <img
              src={`https://api.qrserver.com/v1/create-qr-code/?size=180x180&data=${encodeURIComponent(setupData.uri)}`}
              alt={t("page.settings.2fa_qr_code")}
              style={{ width: 180, height: 180, borderRadius: 12, border: "0.5px solid rgba(29, 29, 31,0.06)" }}
            />
          </div>
          <label className="manor-label">{t("page.settings.or_enter_secret_manually")}</label>
          <code style={{ display: "block", fontSize: 13, background: "var(--surface-muted)", padding: 10, borderRadius: 10, userSelect: "all", fontFamily: "var(--font-mono)", marginBottom: 12, fontWeight: 700 }}>{setupData.secret}</code>
          <label className="manor-label">{t("page.settings.6_digit_code")}</label>
          <input className="manor-input" value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))} placeholder="000000" maxLength={6} style={{ width: 160, fontFamily: "var(--font-mono)", }} />
          {error && <p style={{ fontSize: 13, color: "#c14a44", margin: "8px 0" }}>{error}</p>}
          <div style={{ display: "flex", gap: 8, marginTop: 12 }}><button className="btn-manor" onClick={handleVerify} disabled={code.length < 6}>{t("page.settings.verify_enable")}</button><button className="btn-manor-ghost" onClick={() => { setStep("idle"); setCode(""); setSetupData(null); setError(""); }}>{t("action.cancel")}</button></div>
        </div>
      )}
      {step === "disable" && (
        <div style={{ borderTop: "0.5px solid rgba(29, 29, 31,0.06)", paddingTop: 16, marginTop: 16 }}>
          <label className="manor-label">{t("page.settings.enter_current_totp_code_to_disable")}</label>
          <input className="manor-input" value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))} placeholder="000000" maxLength={6} style={{ width: 160, fontFamily: "var(--font-mono)", }} />
          {error && <p style={{ fontSize: 13, color: "#c14a44", margin: "8px 0" }}>{error}</p>}
          <div style={{ display: "flex", gap: 8, marginTop: 12 }}><button className="btn-manor-danger" onClick={handleDisable} disabled={code.length < 6}>{t("page.settings.confirm_disable")}</button><button className="btn-manor-ghost" onClick={() => { setStep("idle"); setCode(""); setError(""); }}>{t("action.cancel")}</button></div>
        </div>
      )}
      {backupCodes && (
        <div style={{ borderTop: "0.5px solid rgba(29, 29, 31,0.06)", paddingTop: 16, marginTop: 16 }}>
          <p style={{ fontSize: 13, fontWeight: 600, color: "#c14a44", marginBottom: 8 }}>{t("page.settings.save_backup_codes")}</p>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))", gap: 4, background: "#f3ecd6", padding: 12, borderRadius: 10, border: "1px solid #ddbb63" }}>
            {backupCodes.map((bc, i) => <code key={i} style={{ fontSize: 12, fontFamily: "var(--font-mono)", color: "#78350f" }}>{bc}</code>)}
          </div>
          <button className="btn-manor-teal-light" onClick={() => setBackupCodes(null)} style={{ marginTop: 10, fontSize: 12 }}>{t("page.settings.saved_backup_codes")}</button>
        </div>
      )}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   Security — connected apps and agents (OAuth sign-ins with Manor)
   ═══════════════════════════════════════════════════════════════════ */

/** One row's secondary line: when it connected and was last used, plus a key's prefix and expiry. */
function connectionMeta(c: OAuthConnection): string {
  if (c.kind !== "personal_token") {
    return t("page.settings.connected_agent_meta", {
      connected: formatDateOnly(c.connected_at),
      last: c.last_used_at ? relativeTime(c.last_used_at) : "—",
    });
  }
  return t("page.settings.access_key_meta", {
    prefix: `${c.token_prefix ?? ""}…`,
    created: formatDateOnly(c.connected_at),
    last: c.last_used_at ? relativeTime(c.last_used_at) : t("page.settings.access_key_never_used"),
    expiry: c.expires_at
      ? t("page.settings.access_key_expires_on", { date: formatDateOnly(c.expires_at) })
      : t("page.settings.access_key_never_expires"),
  });
}

function accessKeyExpiryOptions() {
  return [
    ...[30, 90, 365].map((days) => ({ value: String(days), label: t("page.settings.access_key_expiry_days", { days }) })),
    { value: "never", label: t("page.settings.access_key_never_expires") },
  ];
}

function CreateAccessKeyModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const [name, setName] = useState("");
  const [expiry, setExpiry] = useState("90");
  const [readOnly, setReadOnly] = useState(false);
  const [created, setCreated] = useState<PersonalAccessTokenCreated | null>(null);
  const create = useMutation({
    mutationFn: () => api.oauthConnections.createAccessKey({
      name: name.trim(),
      scope: readOnly ? "mcp:read" : "mcp",
      expires_in_days: expiry === "never" ? null : Number(expiry),
    }),
    onSuccess: (key) => {
      setCreated(key);
      queryClient.invalidateQueries({ queryKey: ["oauth-connections"] });
    },
    onError: (e: any) => toast.error(t("page.settings.access_key_create_failed"), String(e?.message || e)),
  });
  const close = () => {
    setName("");
    setExpiry("90");
    setReadOnly(false);
    setCreated(null);
    onClose();
  };
  const copy = (text: string) => navigator.clipboard?.writeText(text).then(
    () => toast.success(t("page.developer.copied")),
    () => toast.error(t("page.developer.copy_failed")),
  );
  const endpoint = `${window.location.origin}/api/v1/mcp`;

  return (
    <Modal
      open={open}
      onClose={close}
      title={created ? t("page.settings.access_key_created_title") : t("page.settings.access_key_create")}
      maxWidth="480px"
      footer={created ? (
        <Button onClick={close}>{t("page.settings.access_key_done")}</Button>
      ) : (
        <>
          <Button variant="ghost" onClick={close}>{t("action.cancel")}</Button>
          <Button onClick={() => create.mutate()} loading={create.isPending} disabled={!name.trim()}>
            {t("page.settings.access_key_create")}
          </Button>
        </>
      )}
    >
      {created ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <p style={{ margin: 0, fontSize: 13, color: "var(--text-muted)" }}>{t("page.settings.access_key_copy_now")}</p>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <code className="mono" style={{ flex: 1, minWidth: 0, fontSize: 12, padding: "8px 10px", background: "rgba(29, 29, 31,0.04)", borderRadius: 8, wordBreak: "break-all" }}>
              {created.token}
            </code>
            <Button size="sm" variant="outline" onClick={() => copy(created.token)}>{t("page.developer.copy")}</Button>
          </div>
          <p style={{ margin: 0, fontSize: 12, color: "var(--text-muted)", lineHeight: 1.55 }}>
            {t("page.settings.access_key_usage", { endpoint })}
          </p>
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <p style={{ margin: 0, fontSize: 13, color: "var(--text-muted)", lineHeight: 1.55 }}>
            {t("page.settings.access_key_create_desc")}
          </p>
          <Input
            label={t("page.settings.access_key_name")}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={t("page.settings.access_key_name_placeholder")}
            maxLength={100}
            autoFocus
          />
          <div>
            <label className="manor-label">{t("page.settings.access_key_expiry")}</label>
            <Select value={expiry} onChange={setExpiry} options={accessKeyExpiryOptions()} ariaLabel={t("page.settings.access_key_expiry")} />
          </div>
          <Checkbox
            checked={readOnly}
            onChange={setReadOnly}
            size="sm"
            label={<span style={{ fontSize: 13 }}>{t("page.settings.access_key_read_only")}</span>}
          />
        </div>
      )}
    </Modal>
  );
}

function ConnectedAgentsSection() {
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const [pending, setPending] = useState<OAuthConnection | null>(null);
  const [creating, setCreating] = useState(false);
  const { data: connections, isLoading } = useQuery({
    queryKey: ["oauth-connections"],
    queryFn: () => api.oauthConnections.list(),
  });
  const revoke = useMutation({
    mutationFn: (id: string) => api.oauthConnections.revoke(id),
    onSuccess: () => {
      toast.success(t("page.settings.connected_agent_revoked"));
      setPending(null);
    },
    onError: (e: any) => toast.error(t("page.settings.connected_agent_revoke_failed"), String(e?.message || e)),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["oauth-connections"] }),
  });

  return (
    <div className="glass-card" style={{ padding: "24px 28px" }}>
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
        <div style={{ minWidth: 0, flex: "1 1 280px" }}>
          <h4 className="manor-section-title" style={{ margin: 0 }}>{t("page.settings.connected_agents")}</h4>
          <p className="manor-section-subtitle" style={{ marginTop: 4 }}>{t("page.settings.connected_agents_desc")}</p>
        </div>
        <Button size="sm" variant="outline" style={{ flexShrink: 0, whiteSpace: "nowrap" }} onClick={() => setCreating(true)}>
          {t("page.settings.access_key_create")}
        </Button>
      </div>
      {isLoading ? (
        <ListRowsSkeleton rows={2} action />
      ) : !connections?.length ? (
        <EmptyState compact title={t("page.settings.connected_agents_empty")} />
      ) : (
        <div style={{ marginTop: 8 }}>
          {connections.map((c, i) => (
            <div
              key={c.id}
              style={{
                display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12,
                padding: "12px 0", borderTop: i ? "0.5px solid rgba(29, 29, 31,0.06)" : undefined,
              }}
            >
              <div style={{ minWidth: 0 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ fontWeight: 600, color: "var(--text-strong)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{c.name}</span>
                  {c.kind === "personal_token" && <Chip size="sm" variant="slate">{t("page.settings.access_key")}</Chip>}
                  {c.scope === "mcp:read" && <Chip size="sm" variant="slate">{t("page.settings.access_key_read_only_chip")}</Chip>}
                  {!c.verified && <Chip size="sm" variant="slate">{t("page.settings.connected_agent_unverified")}</Chip>}
                </div>
                <div className="mono" style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>
                  {connectionMeta(c)}
                </div>
              </div>
              <Button size="sm" variant="outline" onClick={() => setPending(c)}>
                {t("page.settings.connected_agent_revoke")}
              </Button>
            </div>
          ))}
        </div>
      )}
      <ConfirmDialog
        open={pending !== null}
        onClose={() => setPending(null)}
        onConfirm={() => pending && revoke.mutate(pending.id)}
        closeOnConfirm={false}
        loading={revoke.isPending}
        danger
        title={t("page.settings.connected_agent_revoke_title").replace("{name}", pending?.name ?? "")}
        message={pending?.kind === "personal_token"
          ? t("page.settings.access_key_revoke_message")
          : t("page.settings.connected_agent_revoke_message")}
        confirmLabel={t("page.settings.connected_agent_revoke")}
        cancelLabel={t("action.cancel")}
      />
      <CreateAccessKeyModal open={creating} onClose={() => setCreating(false)} />
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════
   Main Settings Page
   ═══════════════════════════════════════════════════════════════════ */

type SettingsTabKey =
  | "appearance"
  | "team"
  | "clients"
  | "integrations"
  | "agents"
  | "memories"
  | "automations"
  | "history"
  | "skills"
  | "flows"
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
  /** Pages with their own routes (team, clients, integrations) open at their
   *  address inside the same Settings frame. */
  path?: string;
};

type SettingsNavGroup = {
  label: string;
  items: SettingsNavItem[];
};

const PERSONAL_SETTINGS: SettingsNavItem[] = [
  {
    key: "appearance",
    label: t("page.account.appearance"),
    description: "Theme, AI Edit display, and interface preferences.",
    icon: IconPalette,
  },
  {
    key: "calendar",
    label: t("page.settings.tab_calendar"),
    description: "Connect booking links and availability for scheduled work.",
    icon: IconCalendar,
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

// The company: its people, its clients and the accounts it has connected.
const COMPANY_SETTINGS: SettingsNavItem[] = [
  { key: "team", label: t("nav.team"), description: "Members and roles.", icon: IconPeople, path: "/team" },
  { key: "clients", label: t("page.settings.tab_clients"), description: "Client relationships and follow-ups.", icon: IconBuilding, path: CLIENTS_PATH },
  { key: "integrations", label: t("nav.integrations"), description: "Connected accounts and tools.", icon: IconConnection, path: "/integrations" },
];

// AI employees, what they can do and how they work together live here: they
// are built and tuned now and then, not visited every day (no sidebar place).
const AI_SETTINGS: SettingsNavItem[] = [
  {
    key: "agents",
    label: t("nav.agents"),
    description: "AI employees, their prompts, tools and marketplace.",
    icon: IconAgent,
  },
  {
    key: "skills",
    label: t("nav.skills"),
    description: "Company skill library.",
    icon: IconSkill,
  },
  {
    key: "memories",
    label: t("ui.memories"),
    description: t("page.memories.subtitle"),
    icon: IconBookmark,
  },
  {
    key: "flows",
    label: t("nav.flows"),
    description: "Workflow library and templates.",
    icon: IconFlow,
  },
  {
    key: "automations",
    label: t("ui.automations"),
    description: t("page.scheduled_jobs.automations_subtitle"),
    icon: IconClock,
  },
  {
    key: "history",
    label: t("nav.chatHistory"),
    description: t("page.chat_history.settings_description"),
    icon: IconChat,
  },
  {
    key: "ai",
    label: t("page.settings.tab_ai_models"),
    description: "Choose models for reasoning, worker jobs, and media generation.",
    icon: IconBrain,
  },
  {
    key: "files",
    label: t("approval_settings.title"),
    description: t("approval_settings.description"),
    icon: IconCheckCircle,
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

const EMBEDDED_PAGE_TABS = new Set<SettingsTabKey>(["agents", "skills", "memories", "flows", "automations", "history"]);

/** Routed pages shown inside the Settings frame (see SettingsFrame). */
export const SETTINGS_ROUTED_PATHS = ["/team", "/integrations", "/computers"];

function useSettingsGroups() {
  const user = useAuthStore((s) => s.user);
  const isAdmin = user?.role === "admin" || user?.role === "owner";
  let showBilling = false;
  const flowsAccess = usePreviewFeatureAccess("flows");
  const aiItems = (...keys: SettingsTabKey[]) => AI_SETTINGS.filter((item) => keys.includes(item.key));
  let runtimeItems = aiItems("ai", "files");
  const groups: SettingsNavGroup[] = [
    { label: t("page.settings.group_personal"), items: PERSONAL_SETTINGS },
    { label: t("page.settings.group_company"), items: COMPANY_SETTINGS },
    // Who does the work, how it runs, and where and under what limits.
    { label: t("page.settings.group_ai"), items: aiItems("agents", "skills", "memories", "history") },
    { label: t("page.settings.group_process"), items: flowsAccess.enabled ? aiItems("flows", "automations") : aiItems("automations") },
    { label: t("page.settings.group_runtime"), items: runtimeItems },
    ...(isAdmin && SHOW_DEVELOPER_TAB
      ? [{ label: t("ui.advanced"), items: [DEVELOPER_SETTINGS] }]
      : []),
  ];
  return { groups, flowsEnabled: flowsAccess.enabled };
}

/**
 * The Settings window: its sidebar on the left, one item's page on the
 * right. Routed pages (team, clients, integrations) render inside it too, and
 * their PageHeader lands at the top of the panel instead of the app header.
 */
export function SettingsFrame({
  activeKey,
  onSelect,
  heading,
  children,
}: {
  activeKey: SettingsTabKey;
  onSelect?: (key: SettingsTabKey) => void;
  /** A heading for panels that do not bring their own PageHeader. */
  heading?: string;
  children: ReactNode;
}) {
  const location = useLocation();
  const navigate = useNavigate();
  const { groups } = useSettingsGroups();
  const [settingsSearch, setSettingsSearch] = useState("");
  const userId = useAuthStore((s) => s.user?.id);
  const clientsDiscovery = useFeatureDiscovery({
    feature: FEATURE_DISCOVERIES.clients,
    userId,
    alreadyUsed: false,
    viewed: location.pathname === CLIENTS_PATH,
    autoShow: false,
  });
  const normalizedSearch = settingsSearch.trim().toLowerCase();
  const visibleGroups = groups
    .map((group) => ({
      ...group,
      items: group.items.filter((item) => {
        if (!normalizedSearch) return true;
        return `${item.label} ${item.description} ${group.label}`.toLowerCase().includes(normalizedSearch);
      }),
    }))
    .filter((group) => group.items.length > 0);
  const returnTo = location.state?.returnTo;
  const backToApp = typeof returnTo === "string" && returnTo.startsWith("/")
    && !returnTo.startsWith("//") && !returnTo.startsWith("/settings")
    && !SETTINGS_ROUTED_PATHS.some((path) => returnTo.startsWith(path))
    ? returnTo
    : "/chat";
  const select = (item: SettingsNavItem) => {
    if (item.path) {
      navigate(item.path, { state: location.state });
      return;
    }
    if (onSelect) onSelect(item.key);
    else navigate(`/settings?tab=${encodeURIComponent(item.key)}`, { state: location.state });
  };

  return (
    <div className="settings-page">
      <div className="settings-shell">
        <aside className="settings-local-sidebar" aria-label={t("ui.settings_navigation")}>
          <button
            type="button"
            className="settings-back-to-app"
            onClick={() => navigate(backToApp)}
          >
            <IconArrowLeft size={16} aria-hidden />
            {t("page.settings.back_to_app")}
          </button>

          <label className="settings-sidebar-search">
            <IconSearch size={15} />
            <input
              value={settingsSearch}
              onChange={(event) => setSettingsSearch(event.target.value)}
              placeholder={t("page.settings.search_settings")}
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
                        className={`settings-sidebar-item${item.key === activeKey ? " is-active" : ""}`}
                        aria-current={item.key === activeKey ? "page" : undefined}
                        onClick={() => select(item)}
                      >
                        <span className="settings-sidebar-item-icon"><Icon size={16} /></span>
                        <span className="settings-sidebar-item-copy">
                          <span>{item.label}</span>
                        </span>
                        {item.key === "clients" && clientsDiscovery.badgeVisible && (
                          <FeatureDiscoveryBadge label={t("component.feature_discovery.new")} />
                        )}
                      </button>
                    );
                  })}
                </div>
              </div>
            ))}
            {visibleGroups.length === 0 && (
              <div className="settings-sidebar-empty">{t("page.settings.no_settings_found")}</div>
            )}
          </div>
        </aside>

        <main className="settings-detail">
          {/* Just the item name: the sidebar already says where you are.
              Panels with their own header (search, "+") skip it. */}
          {heading && (
            <div className="settings-detail-header">
              <h2>{heading}</h2>
            </div>
          )}

          <div className="settings-content-panel">
            <PageHeaderBoundary>
              <Suspense fallback={<PanelLoading rows={4} minHeight={240} />}>
                {children}
              </Suspense>
            </PageHeaderBoundary>
          </div>
        </main>
      </div>
    </div>
  );
}

export default function Settings() {
  const location = useLocation();
  const user = useAuthStore((s) => s.user);
  const isAdmin = user?.role === "admin" || user?.role === "owner";
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { groups: settingsGroups, flowsEnabled } = useSettingsGroups();


  // Routed items live at their own address; ?tab= only selects in-page panels.
  const settingsItems = settingsGroups.flatMap((group) => group.items).filter((item) => !item.path);

  const defaultTab: SettingsTabKey =
    "appearance";
  const requestedTab = searchParams.get("tab") || defaultTab;
  const routedItem = settingsGroups.flatMap((group) => group.items).find((item) => item.key === requestedTab && item.path);
  const initialTab = requestedTab as SettingsTabKey;
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

  const handleTabChange = (nextTab: SettingsTabKey) => {
    setTab(nextTab);
    navigate(`/settings?tab=${encodeURIComponent(nextTab)}`, { replace: true, state: location.state });
  };

  const content = (() => {
    if (tab === "appearance") return <AppearanceSection embedded />;
    if (tab === "agents") return <Agents embedded />;
    if (tab === "skills") return <Skills embedded />;
    if (tab === "memories") return <Memories embedded />;
    if (tab === "automations") return <ScheduledJobs />;
    if (tab === "history") return <ChatHistory key={location.search} />;
    if (tab === "flows" && flowsEnabled) return <Flows embedded />;
    if (tab === "notifications") return <NotificationsTab />;
    if (tab === "calendar" && user) return <div className="settings-sheet"><CalendarBookingSection user={user} /></div>;
    if (tab === "ai" && user) return <AIModelSection user={user} settingsSurface />;
    if (tab === "files") return <div className="settings-sheet"><FilePermissionSection /></div>;
    if (tab === "security") {
      return (
        <div className="settings-sheet" style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <PasswordSection />
          <SecurityTab />
          <ConnectedAgentsSection />
        </div>
      );
    }
    if (tab === "developer" && isAdmin && SHOW_DEVELOPER_TAB) return <div className="settings-sheet"><DeveloperTab /></div>;
    return null;
  })();

  // ?tab=team and friends: their page lives at its own address.
  if (routedItem?.path) return <Navigate to={routedItem.path} replace state={location.state} />;


  return (
    <SettingsFrame
      activeKey={activeItem.key}
      onSelect={handleTabChange}
      heading={EMBEDDED_PAGE_TABS.has(activeItem.key) ? undefined : activeItem.label}
    >
      {content}
    </SettingsFrame>
  );
}
