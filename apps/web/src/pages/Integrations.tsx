import { useEffect, useState } from "react";
import {
  siNotion,
  siGmail,
  siDiscord,
  siGithub,
  siStripe,
  siPaypal,
  siShopify,
  siWoocommerce,
  siGooglecalendar,
  siGoogledrive,
  siWhatsapp,
  siTelegram,
  siX,
  siYoutube,
  siTiktok,
  siFacebook,
  siSquare,
  siWechat,
  siQuickbooks,
  siGooglesheets,
  siGoogledocs,
  type BrandIcon,
} from "../lib/brandIcons";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type WorkerResponse } from "../lib/api";
import { useToastStore } from "../stores/toast";
import { useAuthStore } from "../stores/auth";
import { MANOR_AGENT_ID } from "../lib/constants";
import PageHeader from "../components/ui/PageHeader";
import NangoConnectButton from "../components/integrations/NangoConnectButton";
import TabSwitcher from "../components/ui/TabSwitcher";
import SmartToolbar from "../components/ui/SmartToolbar";
import Modal from "../components/ui/Modal";
import ConfirmDialog from "../components/ui/ConfirmDialog";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import Button from "../components/ui/Button";
import Input from "../components/ui/Input";
import Textarea from "../components/ui/Textarea";
import Chip from "../components/ui/Chip";
import Dropdown from "../components/ui/Dropdown";
import Select from "../components/ui/Select";
import Card from "../components/ui/Card";
import CompactCard from "../components/ui/CompactCard";
import IconTile from "../components/ui/IconTile";
import AgentAvatar from "../components/ui/AgentAvatar";
import { openDetail, closeDetail, useDetailStore } from "../stores/detail";
import InfoPopover from "../components/ui/InfoPopover";

import { t } from "../lib/i18n";
// Backend sentinel for "this secret is stored; don't change it". See
// _SECRET_MASK / credential_preview in apps/api/routers/integrations.py.
const UNCHANGED = "__unchanged__";



import {
  IconCalendar,
  IconFolder,
  IconDocument,
  IconChat,
  IconWebhook,
  IconDollar,
  IconCheckCircle,
  IconWarning,
  IconSettings,
  IconExternalLink,
  IconRefresh,
  IconPause,
  IconPlay,
  IconTerminal,
  IconTrash,
  IconTelegram,
  IconWhatsApp,
  IconWeChat,
  IconSlack,
  IconGitHub,
  IconStripe,
  IconTwilio,
  IconEmail,
  IconCode,
  IconLinkedIn,
  IconTwitter,
  IconFacebook,
  IconYouTube,
  IconTikTok,
  IconShoppingCart,
  IconStore,
  IconBox,
  IconPayPal,
  IconCloud,
  IconExcelGrid,
  IconGlobe,
  type IconProps,
} from "../components/icons";

/* ------------------------------------------------------------------ */
/*  Types & constants                                                  */
/* ------------------------------------------------------------------ */

type Tab = "agents" | "channels";
type IntegrationAudience =
  | "all"
  | "cloud"
;
type IntegrationDisplayCategoryKey =
  | "communication"
  | "work_apps"
  | "commerce_payments"
  | "developer_tools"
  | "ai_media"
  | "other";

const INTEGRATION_DISPLAY_CATEGORIES: Record<
  IntegrationDisplayCategoryKey,
  { labelKey: string; Icon: (props: IconProps) => JSX.Element; rank: number }
> = {
  communication: {
    labelKey: "page.integrations.category_communication",
    Icon: IconChat,
    rank: 20,
  },
  work_apps: {
    labelKey: "page.integrations.category_work_apps",
    Icon: IconFolder,
    rank: 30,
  },
  commerce_payments: {
    labelKey: "page.integrations.category_commerce_payments",
    Icon: IconDollar,
    rank: 40,
  },
  developer_tools: {
    labelKey: "page.integrations.category_developer_tools",
    Icon: IconCode,
    rank: 50,
  },
  ai_media: {
    labelKey: "page.integrations.category_ai_media",
    Icon: IconCloud,
    rank: 60,
  },
  other: {
    labelKey: "page.integrations.category_other",
    Icon: IconBox,
    rank: 90,
  },
};

const INTEGRATION_CATEGORY_ALIASES: Record<
  string,
  IntegrationDisplayCategoryKey
> = {
  email: "communication",
  messaging: "communication",
  social: "communication",
  marketing: "communication",
  productivity: "work_apps",
  finance: "commerce_payments",
  "e-commerce": "commerce_payments",
  ecommerce: "commerce_payments",
  developer: "developer_tools",
  "ai tools": "ai_media",
};

const INTEGRATION_SERVER_CATEGORY_OVERRIDES: Record<
  string,
  IntegrationDisplayCategoryKey
> = {
  _google_workspace: "work_apps",
  tavily: "developer_tools",
  github: "developer_tools",
  webhook: "developer_tools",
  replicate: "ai_media",
  elevenlabs: "ai_media",
  jimeng: "ai_media",
  midjourney_web: "ai_media",
  notebooklm: "ai_media",
  claude_ai_web: "ai_media",
  chatgpt_web: "ai_media",
  gemini_web: "ai_media",
  perplexity_web: "ai_media",
};

/* ------------------------------------------------------------------ */
/*  Component                                                          */
/* ------------------------------------------------------------------ */

export default function Integrations() {
  const [tab, setTab] = useState<Tab>("agents");
  const [integrationSearch, setIntegrationSearch] = useState("");

  // Shared: count of bound channels for the tab badge
  const { data: bindings } = useQuery({
    queryKey: ["channel-bindings"],
    queryFn: () => api.integrations.channelBindings(),
  });
  const boundCount = (bindings || []).filter((b) => b.bound_agent_id).length;

  const tabs = [
    {
      key: "agents",
      label: t("page.integrations.for_agents"),
      count: undefined,
    },
    {
      key: "channels",
      label: t("page.integrations.agent_channels"),
      count: boundCount,
    },
  ];

  return (
    <div
      style={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
        padding: "clamp(0.5rem, 2.5vw, 1rem)",
        overflow: "hidden",
        position: "relative",
        zIndex: 10,
      }}
    >
      {/* Header */}
      <PageHeader
        title={t("nav.integrations")}
        subtitle={t("page.integrations.connect_external_services_and_tools")}
        tabs={
          <TabSwitcher
            tabs={tabs}
            value={tab}
            onChange={(k) => setTab(k as Tab)}
          />
        }
        toolbar={
          <SmartToolbar
            searchValue={integrationSearch}
            onSearchChange={setIntegrationSearch}
            searchPlaceholder={t("page.integrations.search_integrations")}
            className="w-full sm:w-64"
          />
        }
      />

      {/* ═══ FOR AGENTS (MCP) TAB ═══ */}
      {tab === "agents" && (
        <MCPAgentsPanel
          search={integrationSearch}
          onClearSearch={() => setIntegrationSearch("")}
        />
      )}

      {/* ═══ AGENT CHANNELS TAB ═══ */}
      {tab === "channels" && <ChannelBindingsPanel />}
    </div>
  );
}

/* ══════════════════════════════════════════════════════════════════════════
   MCPAgentsPanel — live view of the 8 MCP servers with per-user + per-entity
   connection status. Agents use these servers to act on behalf of the
   current user via tool_pool → agent_permission_service → mcp module.
   ══════════════════════════════════════════════════════════════════════════ */

/** Map server_key → brand icon from the global icon library. Each
 *  provider gets its real logo where we have one; fall back to a generic
 *  functional icon (and, as a final fallback, a monogram) otherwise. */
const MCP_LOGO_COLOR: Record<string, string> = {
  gmail: "#EA4335",
  google_calendar: "#4285F4",
  google_drive: "#0F9D58",
  slack: "#4A154B",
  discord: "#5865F2",
  telegram: "#229ED9",
  wechat_personal: "#07C160",
  wechat_official: "#07C160",
  whatsapp: "#25D366",
  twilio: "#F22F46",
  linkedin: "#0A66C2",
  linkedin_browser: "#0A66C2", // same brand colour as the OAuth card
  twitter_x: "#111111",
  github: "#181717",
  webhook: "#57534e",
  quickbooks: "#2CA01C",
  stripe: "#635BFF",
  paypal: "#003087",
  facebook: "#1877F2",
  youtube: "#FF0000",
  tiktok: "#111111",
  shopify: "#96BF48",
  woocommerce: "#7F54B3",
  square: "#3E4348",
  tiktok_shop: "#FE2C55",
  amazon: "#FF9900",
  email: "#78716c",
  notion: "#111111",
  // Microsoft 365 — Outlook + OneDrive + MS Calendar in MS blue,
  // Teams in MS purple, Excel in MS green. All five share one Azure
  // AD app registration but render with their own product brand.
  outlook: "#0078D4",
  onedrive: "#0364B8",
  ms_calendar: "#0078D4",
  ms_teams: "#6264A7",
  ms_excel: "#107C41",
};

function getLogoColor(serverKey?: string | null, fallback = "#78716c") {
  if (!serverKey) return fallback;
  return MCP_LOGO_COLOR[serverKey] || fallback;
}

const MCP_ICON: Record<string, (p: IconProps) => JSX.Element> = {
  gmail: IconEmail,
  email: IconEmail,
  google_calendar: IconCalendar,
  google_drive: IconFolder,
  notion: IconDocument,
  slack: IconSlack,
  discord: IconChat,
  telegram: IconTelegram,
  wechat: IconWeChat,
  wechat_personal: IconWeChat,
  wechat_official: IconWeChat,
  whatsapp: IconWhatsApp,
  twilio: IconTwilio,
  linkedin: IconLinkedIn,
  linkedin_browser: IconLinkedIn, // same logo as the OAuth card
  twitter_x: IconTwitter,
  github: IconGitHub,
  webhook: IconWebhook,
  quickbooks: IconDollar,
  stripe: IconStripe,
  paypal: IconPayPal,
  facebook: IconFacebook,
  youtube: IconYouTube,
  tiktok: IconTikTok,
  // E-commerce — generic commerce glyphs coloured by MCP_LOGO_COLOR.
  shopify: IconShoppingCart,
  woocommerce: IconStore,
  square: IconBox,
  tiktok_shop: IconShoppingCart,
  amazon: IconStore,
  // Microsoft 365 — generic icons coloured by MCP_LOGO_COLOR. Excel
  // gets its own grid mark to read clearly at 16-20px.
  outlook: IconEmail,
  onedrive: IconCloud,
  ms_calendar: IconCalendar,
  ms_teams: IconChat,
  ms_excel: IconExcelGrid,
};

/* Official brand marks from simple-icons (CC0). Providers simple-icons has
 * removed at the brand's request (Slack, LinkedIn, Amazon, Twilio, …) fall
 * back to the in-house MCP_ICON below. */
const BRAND_SI: Record<string, BrandIcon> = {
  gmail: siGmail,
  email: siGmail,
  notion: siNotion,
  discord: siDiscord,
  github: siGithub,
  stripe: siStripe,
  paypal: siPaypal,
  shopify: siShopify,
  woocommerce: siWoocommerce,
  google_calendar: siGooglecalendar,
  google_drive: siGoogledrive,
  google_sheets: siGooglesheets,
  google_docs: siGoogledocs,
  whatsapp: siWhatsapp,
  telegram: siTelegram,
  twitter_x: siX,
  youtube: siYoutube,
  tiktok: siTiktok,
  tiktok_shop: siTiktok,
  facebook: siFacebook,
  square: siSquare,
  wechat: siWechat,
  wechat_personal: siWechat,
  wechat_official: siWechat,
  quickbooks: siQuickbooks,
};

/**
 * IntegrationLogo — official brand logo for an integration. Uses the
 * simple-icons (CC0) mark where available, in the brand colour; otherwise
 * renders the provided fallback (in-house icon / monogram).
 */
function IntegrationLogo({
  serverKey,
  size,
  fallback,
}: {
  serverKey: string;
  size: number;
  fallback: React.ReactNode;
}) {
  const si = BRAND_SI[serverKey];
  if (si) {
    return (
      <svg
        role="img"
        viewBox="0 0 24 24"
        width={size}
        height={size}
        fill={`#${si.hex}`}
        style={{ display: "block" }}
        aria-hidden
      >
        <path d={si.path} />
      </svg>
    );
  }
  return <>{fallback}</>;
}

const CARD_SECTION_HEIGHT = {
  header: 88,
  body: 92,
} as const;

function _relTime(iso?: string | null) {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (!then) return "—";
  const diff = Math.max(0, Date.now() - then) / 1000;
  if (diff < 10) return t("common.time.just_now");
  if (diff < 60) return `${Math.floor(diff)}s`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
  if (diff < 604800) return `${Math.floor(diff / 86400)}d`;
  return `${Math.floor(diff / 604800)}w`;
}

const _CHANNEL_LABEL: Record<string, string> = {
  wechat: t("page.integrations.wechat_oa"),
  wechat_personal: t("page.integrations.wechat_personal"),
  whatsapp: t("page.integrations.whatsapp"),
  slack: t("page.integrations.slack"),
  discord: t("page.integrations.discord"),
  email: t("page.integrations.email"),
  internal_chat: t("page.workspace_detail.channel_kind_internal"),
  webchat: t("page.workspace_detail.channel_kind_webchat"),
  twilio_sms: t("page.integrations.twilio_sms"),
  twilio_voice: t("page.integrations.twilio_voice"),
  inapp: t("page.integrations.in_app"),
  sms: t("page.integrations.sms"),
  voice: t("page.integrations.voice"),
  facebook: t("page.integrations.facebook"),
};

/* ══════════════════════════════════════════════════════════════════════════
   Design-system shims — small reusable wrappers that collapse repeated
   inline-style patterns into on-system primitives. Scoped to this page;
   promote to /components/ui if any get reused elsewhere.
   ══════════════════════════════════════════════════════════════════════════ */

/** Small teal pill used on rows to mark the default account. */
function DefaultBadge() {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        fontSize: 9,
        fontWeight: 700,
        padding: "1px 6px",
        borderRadius: 4,
        background: "#e7e5e4",
        color: "#44403c",
        textTransform: "uppercase" as const,
        letterSpacing: "0.05em",
        flexShrink: 0,
      }}
    >
      {t("page.api_keys.default")}
    </span>
  );
}

/** Red pill for an expired OAuth token. */
function ExpiredBadge() {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        fontSize: 9,
        fontWeight: 700,
        padding: "1px 6px",
        borderRadius: 4,
        background: "#f5f5f4",
        color: "#78716c",
        textTransform: "uppercase" as const,
        letterSpacing: "0.05em",
        flexShrink: 0,
      }}
    >
      {t("page.integrations.expired")}
    </span>
  );
}

/** The `⋯` dropdown trigger used on every row action menu. */
function MoreMenu<K extends string = string>({
  items,
  onSelect,
}: {
  items: {
    key: K;
    label: string;
    danger?: boolean;
    disabled?: boolean;
    icon?: React.ReactNode;
  }[];
  onSelect: (key: K) => void;
}) {
  return (
    <Dropdown
      align="right"
      trigger={
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            width: 24,
            height: 24,
            borderRadius: 4,
            color: "#a8a29e",
            fontSize: 14,
            fontWeight: 700,
            lineHeight: 1,
          }}
        >
          ⋯
        </span>
      }
      items={items}
      onSelect={(k) => onSelect(k as K)}
    />
  );
}

/** Tinted info box used in modals for docs hints / callback URLs. */
function InfoAlert({
  tone = "teal",
  children,
}: {
  tone?: "teal" | "amber";
  children: React.ReactNode;
}) {
  const palette =
    tone === "amber"
      ? { bg: "#fafaf9", border: "#e7e5e4", color: "#57534e" }
      : { bg: "#fafaf9", border: "#e7e5e4", color: "#57534e" };
  return (
    <div
      style={{
        background: palette.bg,
        border: `1px solid ${palette.border}`,
        borderRadius: 10,
        padding: 10,
        fontSize: 11,
        color: palette.color,
        lineHeight: 1.5,
        wordBreak: "break-word" as const,
      }}
    >
      {children}
    </div>
  );
}

/* ══════════════════════════════════════════════════════════════════════════
   ChannelBindingsPanel — the Agent Channels tab. One row per ChannelConfig;
   each row lets the user pick which agent the channel's inbound traffic
   routes to. Powered by /integrations/channel-bindings.
   ══════════════════════════════════════════════════════════════════════════ */

function ChannelBindingsPanel() {
  const queryClient = useQueryClient();
  const toast = useToastStore();

  const { data: bindings, isLoading } = useQuery({
    queryKey: ["channel-bindings"],
    queryFn: () => api.integrations.channelBindings(),
  });

  const { data: agents } = useQuery({
    queryKey: ["agents"],
    queryFn: () => api.agents.list(),
  });

  const upsert = useMutation({
    mutationFn: (v: { channel_config_id: string; agent_id: string | null }) =>
      api.integrations.upsertChannelBinding(v),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["channel-bindings"] });
      toast.success(t("page.integrations.channel_binding_saved"));
    },
    onError: (e: any) =>
      toast.error(t("page.integrations.failed_to_save"), e?.message || ""),
  });

  const unbind = useMutation({
    mutationFn: (channelId: string) =>
      api.integrations.deleteChannelBinding(channelId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["channel-bindings"] });
      toast.success(t("page.integrations.channel_unbound"));
    },
  });

  const totalChannels = bindings?.length ?? 0;
  const boundChannels = (bindings || []).filter((b) => b.bound_agent_id).length;
  const unboundChannels = Math.max(0, totalChannels - boundChannels);
  const orderedBindings = [...(bindings || [])].sort((a, b) => {
    const aNeeds = a.bound_agent_id ? 1 : 0;
    const bNeeds = b.bound_agent_id ? 1 : 0;
    if (aNeeds !== bNeeds) return aNeeds - bNeeds;
    return String(a.display_name || a.name || "").localeCompare(
      String(b.display_name || b.name || ""),
    );
  });

  return (
    <div className="channel-bindings-page">
      <section className="channel-bindings-summary">
        <div className="channel-bindings-summary-copy">
          <div className="channel-bindings-eyebrow">
            {t("page.integrations.channel_routing")}
          </div>
          <h2>{t("page.integrations.agent_channels")}</h2>
          <p>
            {t(
              "page.integrations.route_inbound_messages_from_each_configured_chan",
            )}
          </p>
        </div>
        <div
          className="channel-bindings-stats"
          aria-label={t("page.integrations.agent_channels")}
        >
          <div className="channel-bindings-stat">
            <span>{totalChannels}</span>
            <label>{t("page.integrations.total_channels")}</label>
          </div>
          <div className="channel-bindings-stat channel-bindings-stat--ready">
            <span>{boundChannels}</span>
            <label>{t("page.integrations.routed")}</label>
          </div>
          <div
            className={`channel-bindings-stat ${unboundChannels > 0 ? "channel-bindings-stat--attention" : ""}`}
          >
            <span>{unboundChannels}</span>
            <label>{t("page.integrations.needs_agent")}</label>
          </div>
        </div>
      </section>

      {isLoading ? (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: "64px 0",
            gap: 12,
            color: "#a8a29e",
          }}
        >
          <LoadingSpinner size={20} />
          <span style={{ fontSize: 14 }}>
            {t("page.integrations.loading_channels")}
          </span>
        </div>
      ) : !bindings || bindings.length === 0 ? (
        <EmptyState
          icon={
            <svg
              style={{ width: 32, height: 32, color: "#d6d3d1" }}
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={1}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"
              />
            </svg>
          }
          title={t("page.integrations.no_channels_configured_yet")}
          description={t(
            "page.integrations.configure_a_messaging_account_on_the_for_agents",
          )}
        />
      ) : (
        <div className="channel-bindings-list">
          <div className="channel-bindings-list-head" aria-hidden="true">
            <span>{t("page.workspace_detail.channel_2")}</span>
            <span>{t("page.integrations.routed_agent")}</span>
            <span>{t("page.integrations.recent_activity")}</span>
            <span>{t("page.integrations.routing")}</span>
          </div>
          {orderedBindings.map((b) => (
            <BindingRow
              key={b.channel_config_id}
              binding={b}
              agents={agents || []}
              onAssign={(agentId) =>
                upsert.mutate({
                  channel_config_id: b.channel_config_id,
                  agent_id: agentId,
                })
              }
              onUnbind={() => {
                if (
                  b.bound_channel_id &&
                  confirm(
                    t("page.integrations.unbind_channel_confirm").replace(
                      "{name}",
                      friendlyChannelName(b),
                    ),
                  )
                ) {
                  unbind.mutate(b.bound_channel_id);
                }
              }}
              busy={upsert.isPending || unbind.isPending}
            />
          ))}
        </div>
      )}
    </div>
  );
}

type ChannelBindingView = {
  channel_config_id: string;
  channel_type: string;
  provider: string;
  name: string | null;
  display_name: string;
  status: string;
  bound_channel_id: string | null;
  bound_agent_id: string | null;
  agent_name: string | null;
  last_inbound_at?: string | null;
  last_outbound_at?: string | null;
};

function channelTypeLabel(binding: ChannelBindingView) {
  return (
    _CHANNEL_LABEL[binding.provider] ||
    _CHANNEL_LABEL[binding.channel_type] ||
    binding.channel_type
  );
}

function friendlyChannelName(binding: ChannelBindingView) {
  const typeLabel = channelTypeLabel(binding);
  const raw = (binding.name || binding.display_name || "").trim();
  if (!raw) return typeLabel;

  const normalized = raw.toLowerCase().replace(/\s+/g, " ");
  const rawType = String(binding.channel_type || "").toLowerCase();
  const rawProvider = String(binding.provider || "").toLowerCase();

  if (
    normalized === rawType ||
    normalized === rawProvider ||
    normalized === `${rawProvider}: ${rawType}` ||
    normalized === `internal: ${rawType}` ||
    normalized === `primary_external: ${rawType}`
  ) {
    return typeLabel;
  }

  if (normalized === "internal_chat")
    return t("page.workspace_detail.channel_kind_internal");
  if (normalized === "webchat")
    return t("page.workspace_detail.channel_kind_webchat");
  return raw;
}

function channelDetailText(binding: ChannelBindingView, label: string) {
  const typeLabel = channelTypeLabel(binding);
  const displayName = String(binding.display_name || "").trim();
  if (displayName && displayName !== label && !displayName.includes(":")) {
    return displayName;
  }
  return typeLabel;
}

function BindingRow({
  binding,
  agents,
  onAssign,
  onUnbind,
  busy,
}: {
  binding: ChannelBindingView;
  agents: any[];
  onAssign: (agentId: string | null) => void;
  onUnbind: () => void;
  busy: boolean;
}) {
  const Icon = MCP_ICON[binding.channel_type] || MCP_ICON[binding.provider];
  const iconColor = "#78716c";
  const channelLabel = channelTypeLabel(binding);
  const label = friendlyChannelName(binding);
  const detailText = channelDetailText(binding, label);
  const isBound = !!binding.bound_agent_id;
  const statusColor = isBound ? "#5f928a" : "#c8c1ba";
  const statusLabel = isBound
    ? t("page.integrations.routed_to_agent").replace(
        "{name}",
        binding.agent_name || t("page.agent_dashboard.agent_singular"),
      )
    : t("page.integrations.no_agent_bound");

  // The Manor Master Agent is always available even if the entity
  // hasn't created any custom agents.
  const options = [
    {
      id: MANOR_AGENT_ID,
      name: t("page.integrations.manor_master_agent"),
      role: t("page.integrations.default_role"),
      avatar_url: null,
    },
    ...agents.filter((a) => a.id !== MANOR_AGENT_ID),
  ];
  const assignedAgent = options.find((a) => a.id === binding.bound_agent_id);
  const assignedAgentName =
    binding.agent_name ||
    assignedAgent?.name ||
    t("page.agent_dashboard.agent_singular");
  const activityItems = [
    binding.last_inbound_at
      ? {
          key: "in",
          label: `${t("page.integrations.in")} ${_relTime(binding.last_inbound_at)}`,
          title: new Date(binding.last_inbound_at).toLocaleString(),
        }
      : null,
    binding.last_outbound_at
      ? {
          key: "out",
          label: `${t("page.integrations.out")} ${_relTime(binding.last_outbound_at)}`,
          title: new Date(binding.last_outbound_at).toLocaleString(),
        }
      : null,
  ].filter(Boolean) as Array<{ key: string; label: string; title: string }>;

  return (
    <div
      className={`channel-binding-row ${isBound ? "" : "channel-binding-row--attention"}`}
    >
      <div className="channel-binding-channel-cell">
        <IconTile
          color={iconColor}
          size={34}
          status={{ color: statusColor, label: statusLabel }}
        >
          {Icon ? (
            <Icon size={17} style={{ color: iconColor }} />
          ) : (
            <span style={{ color: iconColor }}>
              {channelLabel.slice(0, 2).toUpperCase()}
            </span>
          )}
        </IconTile>
        <div className="channel-binding-title-block">
          <div className="channel-binding-title-row">
            <span className="channel-binding-title">{label}</span>
            {!isBound && (
              <span className="channel-binding-attention-pill">
                {t("page.integrations.needs_agent")}
              </span>
            )}
          </div>
          <div className="channel-binding-subtitle">{detailText}</div>
        </div>
      </div>

      <div className="channel-binding-agent-cell">
        {isBound ? (
          <span className="channel-binding-agent-pill">
            <AgentAvatar
              name={assignedAgentName}
              avatarUrl={assignedAgent?.avatar_url}
              seed={binding.bound_agent_id || assignedAgentName}
              size={22}
            />
            <span>{assignedAgentName}</span>
          </span>
        ) : (
          <span className="channel-binding-empty-pill">
            {t("page.integrations.not_bound")}
          </span>
        )}
      </div>

      <div className="channel-binding-activity-cell">
        {activityItems.length > 0 ? (
          activityItems.map((item) => (
            <span key={item.key} title={item.title}>
              {item.label}
            </span>
          ))
        ) : (
          <span className="channel-binding-muted">
            {t("page.integrations.no_recent_activity")}
          </span>
        )}
      </div>

      <div className="channel-binding-actions">
        <div className="channel-binding-select-wrap">
          <Select
            value={binding.bound_agent_id || ""}
            onChange={(v) => onAssign(v || null)}
            placeholder={t("page.integrations.bind_to_agent")}
            filterable={options.length > 6}
            showSelectedIcon
            dropdownMinWidth={260}
            style={{
              opacity: busy ? 0.6 : 1,
              pointerEvents: busy ? "none" : "auto",
            }}
            buttonStyle={{
              minHeight: 34,
              borderColor: "rgba(28,25,23,0.07)",
              background: "rgba(255,255,255,0.78)",
              fontSize: 12,
              fontWeight: 600,
            }}
            options={[
              { value: "", label: t("page.integrations.not_bound") },
              ...options.map((a) => ({
                value: a.id,
                label: `${a.name}${a.role ? ` · ${a.role}` : ""}`,
                icon: (
                  <AgentAvatar
                    name={a.name}
                    avatarUrl={a.avatar_url}
                    seed={a.id}
                    size={18}
                  />
                ),
              })),
            ]}
          />
        </div>

        {!isBound && (
          <Button
            variant="primary"
            size="sm"
            disabled={busy}
            onClick={() => onAssign(MANOR_AGENT_ID)}
          >
            {t("page.integrations.use_master")}
          </Button>
        )}

        {binding.bound_channel_id && (
          <Button variant="ghost" size="sm" disabled={busy} onClick={onUnbind}>
            {t("page.skills.unbind")}
          </Button>
        )}
      </div>
    </div>
  );
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}


function MCPAgentsPanel({
  search: externalSearch,
  onClearSearch,
}: {
  search?: string;
  onClearSearch?: () => void;
}) {
  const role = useAuthStore((s) => s.user?.role);
  const authToken = useAuthStore((s) => s.token);
  const authLoading = useAuthStore((s) => s.isLoading);
  const privateApiEnabled = !authLoading && Boolean(authToken);
  const canManage = role === "admin" || role === "owner";
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const [emailConfigFor, setEmailConfigFor] = useState<{
    serverKey: string;
    accountId?: string;
  } | null>(null);
  const [apiKeyConfigFor, setApiKeyConfigFor] = useState<{
    key: string;
    name: string;
    accountId?: string;
  } | null>(null);
  const [oauthConfigFor, setOAuthConfigFor] = useState<{
    key: string;
    name: string;
    scopes?: string | null;
  } | null>(null);
  const [wechatScanFor, setWechatScanFor] = useState<string | null>(null);
  const search = externalSearch ?? "";
  // Top filter is execution model: cloud accounts vs local/user-session tools.
  // Section grouping below stays purpose-based so users do not have to reason
  // about backend auth categories while scanning the catalog.
  const [audience, setAudience] = useState<IntegrationAudience>("all");

  const {
    data: servers,
    isLoading,
    isError,
  } = useQuery({
    queryKey: ["mcp-servers"],
    queryFn: () => api.integrations.mcpServers(),
    enabled: privateApiEnabled,
    retry: 1,
    staleTime: 60_000,
  });

  async function startOAuth(serverKey: string, name: string) {
    try {
      const { authorize_url } = await api.integrations.oauthStart(serverKey);
      // Full redirect so the provider can set its own cookies and bring
      // the user back to /integrations?connected=<server_key> on success
      window.location.href = authorize_url;
    } catch (e: any) {
      useToastStore
        .getState()
        .error(
          t("page.integrations.cant_connect_name").replace("{name}", name),
          e?.status === 501
            ? t("page.integrations.oauth_app_not_configured_for_deployment")
            : e?.message || t("page.integrations.unknown_error"),
        );
    }
  }

  if (isLoading) {
    return (
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          padding: "64px 0",
          gap: 12,
          color: "#a8a29e",
        }}
      >
        <LoadingSpinner size={20} />
        <span style={{ fontSize: 14 }}>
          {t("page.integrations.loading_integrations")}
        </span>
      </div>
    );
  }

  if (isError || (!isLoading && !servers)) {
    return (
      <div style={{ textAlign: "center", padding: "64px 0", color: "#a8a29e" }}>
        <p style={{ fontSize: 14, margin: "0 0 8px" }}>
          {t("page.integrations.failed_to_load_integrations")}
        </p>
        <button
          onClick={() =>
            queryClient.invalidateQueries({ queryKey: ["mcp-servers"] })
          }
          style={{
            fontSize: 13,
            color: "#44403c",
            background: "none",
            border: "none",
            cursor: "pointer",
            textDecoration: "underline",
          }}
        >
          {t("page.dashboard.retry")}
        </button>
      </div>
    );
  }

  // Merge Gmail / Calendar / Drive into one synthetic "Google
  // Workspace" card. They share the same OAuth client and the same
  // user account on Google's side, so 3 separate cards is just
  // visual noise. The synthetic row carries its 3 sub-rows in
  // ``_googleSubs`` so the grid render can swap to
  // GoogleWorkspaceCard.
  const baseRowsRaw = servers || [];
  const GOOGLE_KEYS = ["gmail", "google_calendar", "google_drive"];
  const googleSubs = baseRowsRaw.filter((s) =>
    GOOGLE_KEYS.includes(s.server_key),
  );
  const nonGoogle = baseRowsRaw.filter(
    (s) => !GOOGLE_KEYS.includes(s.server_key),
  );
  // Drop internal automation-only cards from the public integration grid.
  const HIDDEN_INTERNAL_AUTOMATION_KEYS = new Set([
    "local_browser",
    "knowledge_local",
    "chrome_knowledge_local",
  ]);
  const filteredRows = (
    googleSubs.length > 0
      ? [...nonGoogle, buildGoogleGroupRow(googleSubs)]
      : baseRowsRaw
  ).filter((s) => !HIDDEN_INTERNAL_AUTOMATION_KEYS.has(s.server_key));
  let baseRows: McpServerRow[] = filteredRows.filter(
    (s) => s.auth_type !== "cli_worker" && s.auth_type !== "browser_session",
  );
  const rowsWithWorkerEntry: McpServerRow[] = baseRows;

  // Hide unconfigured OAuth cards from non-admins so regular users
  // only see things they can actually click. Admins keep seeing
  // everything (with the "OAuth not configured" chip) so they know
  // which providers still need setup. Connected providers always
  // show regardless of config (so users keep seeing their existing
  // connections even if a refresh later fails).
  const visibleRows = canManage
    ? rowsWithWorkerEntry
    : rowsWithWorkerEntry.filter((s) => {
        const isOAuth = s.auth_type === "oauth2";
        if (!isOAuth) return true;
        const hasConn =
          (s.connections?.length ?? 0) > 0 ||
          (s.entity_accounts?.length ?? 0) > 0;
        // Regular users: show only OAuth-ready or already-connected.
        return s.oauth_configured || s.nango_provider_config_key || hasConn;
      });
  // Runtime-managed cards are hidden from the OSS catalog fallback.
  // SaaS/API cards are the inverse.
  const isRuntimeManagedCard = (s: McpServerRow) =>
    s.auth_type === "cli_worker" || s.auth_type === "browser_session";
  const matchesSearch = (s: McpServerRow, value: string) => {
    const hay = [s.name, s.tagline, s.description, s.category]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return hay.includes(value);
  };
  let localIntegratedRows: McpServerRow[] = [];
  let catalogRows = visibleRows;
  let allRows =
    audience === "all"
      ? catalogRows
      : catalogRows.filter((s) => !isRuntimeManagedCard(s));
  const connectedCount = allRows.filter((s) => s.agent_can_use).length;
  const q = search.trim().toLowerCase();
  const rows = q ? allRows.filter((s) => matchesSearch(s, q)) : allRows;
  let localIntegratedVisibleRows: McpServerRow[] = [];
  let integratedVisibleCount = 0;
  let integratedReadyCount = 0;
  const visibleCatalogCount = allRows.length + integratedVisibleCount;
  const readyCatalogCount = connectedCount + integratedReadyCount;
  let showLocalCodePanel = false;
  const showSearchEmptyState =
    rows.length === 0 && Boolean(q) && !showLocalCodePanel;
  const cloudAudienceCount = visibleRows.filter(
    (s) => !isRuntimeManagedCard(s),
  ).length;
  let localAudienceCount = 0;
  const allAudienceCount = cloudAudienceCount + localAudienceCount;

  const renderServerCard = (s: McpServerRow) =>
    s.server_key === "_google_workspace" ? (
      <GoogleWorkspaceCard
        key="_google_workspace"
        subs={(s as any)._googleSubs as McpServerRow[]}
        canManage={canManage}
        onConnect={(serverKey, name) => void startOAuth(serverKey, name)}
        onConfigureOAuth={(serverKey, name, scopes) =>
          setOAuthConfigFor({ key: serverKey, name, scopes })
        }
      />
    ) : (
      <ServerCard
        key={s.server_key}
        server={s}
        canManage={canManage}
        onConnect={() => void startOAuth(s.server_key, s.name)}
        onConfigureOAuth={() =>
          setOAuthConfigFor({ key: s.server_key, name: s.name, scopes: s.scopes })
        }
        onAddApiKey={() => {
          // Email has its own multi-section modal (IMAP + SMTP
          // presets); every other credential/api_key provider
          // goes through the generic ApiKeyConfigModal driven by API_KEY_FIELDS.
          if (s.server_key === "wechat_personal") {
            // Multi-session iLink scan flow. Spawning a fresh
            // session id key-by-clock means consecutive clicks
            // each open a new scan modal (multi-account).
            setWechatScanFor(`new:${Date.now()}`);
          } else if (s.server_key === "email") {
            setEmailConfigFor({ serverKey: s.server_key });
          } else if (API_KEY_FIELDS[s.server_key]) {
            setApiKeyConfigFor({ key: s.server_key, name: s.name });
          } else {
            useToastStore
              .getState()
              .info(
                t("page.integrations.named_credentials").replace(
                  "{name}",
                  s.name,
                ),
                t("page.integrations.configuration_ui_not_wired"),
              );
          }
        }}
        onEditAccount={(accountId) => {
          if (s.server_key === "email") {
            setEmailConfigFor({ serverKey: s.server_key, accountId });
          } else if (API_KEY_FIELDS[s.server_key]) {
            setApiKeyConfigFor({
              key: s.server_key,
              name: s.name,
              accountId,
            });
          }
        }}
        onScanQr={(accountId) => setWechatScanFor(accountId)}
      />
    );

  return (
    <div style={{ flex: 1, overflowY: "auto", padding: "8px" }}>
      {/* Header strip */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 16,
          marginBottom: 16,
          padding: "14px 18px",
          background: "#ffffff",
          border: "1px solid rgba(28,25,23,0.06)",
          borderRadius: 14,
        }}
      >
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: "#44403c" }}>
            {readyCatalogCount} {t("page.integrations.of")}{" "}
            {visibleCatalogCount}{" "}
            {t("page.integrations.servers_usable_by_your_agents")}
          </div>
          <div style={{ fontSize: 11, color: "#78716c", marginTop: 2 }}>
            {t(
              "page.integrations.connect_a_server_bind_it_in_agent_skills_then_ru",
            )}
          </div>
        </div>
      </div>

      {/* Execution filter — keep both visible for users who want everything in one place. */}
      <div
        style={{
          display: "flex",
          gap: 4,
          marginBottom: 16,
          padding: 4,
          background: "#f5f5f4",
          borderRadius: 10,
          width: "fit-content",
        }}
      >
        {(
          [
            [
              "all",
              t("page.integrations.all_count").replace(
                "{count}",
                String(allAudienceCount),
              ),
            ],
            [
              "cloud",
              t("page.integrations.saas_count").replace(
                "{count}",
                String(cloudAudienceCount),
              ),
            ],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            onClick={() => setAudience(key)}
            style={{
              padding: "6px 14px",
              fontSize: 12,
              fontWeight: 600,
              borderRadius: 7,
              border: "none",
              cursor: "pointer",
              background: audience === key ? "#ffffff" : "transparent",
              color: audience === key ? "#1c1917" : "#78716c",
              boxShadow:
                audience === key ? "0 1px 2px rgba(0,0,0,0.06)" : "none",
            }}
          >
            {label}
          </button>
        ))}
      </div>


      {showSearchEmptyState ? (
        <EmptyState
          title={t("page.integrations.no_integrations_match_your_search")}
          description={t("page.integrations.nothing_found_for_search").replace(
            "{search}",
            search,
          )}
          action={
            <Button variant="outline" onClick={onClearSearch}>
              {t("page.integrations.clear_search")}
            </Button>
          }
        />
      ) : null}

      {/* Group by display category */}
      {groupIntegrationsByDisplayCategory(rows).map(([cat, group]) => (
        <div key={cat} className="integration-section">
          <IntegrationSectionHeader
            categoryKey={cat}
            readyCount={group.filter((g) => g.agent_can_use).length}
            totalCount={group.length}
          />
          <div
            style={{
              display: "grid",
              gridTemplateColumns:
                "repeat(auto-fill, minmax(min(100%, 300px), 1fr))",
              gap: 12,
            }}
          >
            {group.map(renderServerCard)}
          </div>
        </div>
      ))}

      {/* Email (IMAP + SMTP) configuration (entity-level credentials) */}
      <EmailConfigModal
        open={!!emailConfigFor}
        serverKey={emailConfigFor?.serverKey || null}
        accountId={emailConfigFor?.accountId}
        onClose={() => setEmailConfigFor(null)}
      />

      {/* Generic API-key / bearer-token configuration */}
      <ApiKeyConfigModal
        open={!!apiKeyConfigFor}
        target={apiKeyConfigFor}
        onClose={() => setApiKeyConfigFor(null)}
        onSaved={(id, key) => {
          // After saving a wechat_personal account, drop the user straight
          // into the scan panel so they can see the QR without hunting.
          if (key === "wechat_personal") setWechatScanFor(id);
        }}
      />

      <OAuthClientConfigModal
        open={!!oauthConfigFor}
        target={oauthConfigFor}
        onClose={() => setOAuthConfigFor(null)}
      />

      {/* WeChat (Personal) — live QR scan + status */}
      <WeChatPersonalScanModal
        open={!!wechatScanFor}
        accountId={wechatScanFor}
        onClose={() => setWechatScanFor(null)}
      />



    </div>
  );
}

/* ══════════════════════════════════════════════════════════════════════════
   ServerCard — one MCP server tile. Shows brand, status, connected
   accounts (with multi-account support), and action buttons. Built from
   global UI primitives (Chip, Button, global icons).
   ══════════════════════════════════════════════════════════════════════════ */

type McpServerRow = {
  server_key: string;
  name: string;
  category: string | null;
  description: string | null;
  auth_type: string;
  scopes: string | null;
  tagline: string | null;
  docs_url: string | null;
  setup_hint: string | null;
  color_hex: string | null;
  supports_multi_account: boolean;
  connections: Array<{
    id: string;
    display_name: string | null;
    provider_user_id: string;
    expires_at: string | null;
    is_default: boolean;
    connected_at: string | null;
  }>;
  entity_accounts: Array<{
    id: string;
    name: string | null;
    display_name: string | null;
    is_default: boolean;
    created_at: string | null;
    status: string;
    health: {
      ok: boolean | null;
      detail: string | null;
      checked_at: string | null;
      wiring?: {
        ok: boolean | null;
        detail: string | null;
        configured_url?: string | null;
        expected_url?: string | null;
        last_error?: string | null;
        pending_update_count?: number | null;
      } | null;
    } | null;
  }>;
  user_connected: boolean;
  user_expires_at: string | null;
  entity_connected: boolean;
  required_permission: string | null;
  user_has_required_permission: boolean;
  agent_can_use: boolean;
  hint: string;
  /** When set, this provider is configured in our self-hosted Nango.
   *  The Connect button on the card opens Nango's hosted OAuth popup
   *  scoped to this provider — invisible aggregator under the hood. */
  nango_provider_config_key?: string | null;
  /** True when client_id/secret are configured for this OAuth
   *  provider (env-bootstrapped or admin-overridden). False = the
   *  Connect button is disabled and we surface a hint instead. */
  oauth_configured?: boolean;
  /** True when this integration is not yet production-ready.
   *  Single source of truth: backend _COMING_SOON_SERVERS. */
  coming_soon?: boolean;
  /** "What can my agent do once connected?" — surfaced via the ?
   *  info button. Empty list hides the button. */
  capabilities?: string[];
  example_prompts?: string[];
};

/** Per-provider copy for the auth badge + setup button. Falls back to
 *  generic strings if a provider isn't listed. */
const AUTH_LABELS: Record<
  string,
  {
    label: string;
    configureVerb: string;
    chipVariant: "blue" | "teal" | "purple" | "orange" | "green";
  }
> = {
  email: {
    label: t("page.integrations.imap_smtp"),
    configureVerb: t("page.integrations.mail_server"),
    chipVariant: "teal",
  },
  wechat_personal: {
    label: t("page.integrations.clawbot"),
    configureVerb: t("page.integrations.wechat"),
    chipVariant: "green",
  },
  wechat_official: {
    label: t("page.integrations.oa_api"),
    configureVerb: t("page.integrations.official_account"),
    chipVariant: "green",
  },
  webhook: {
    label: t("page.integrations.bearer"),
    configureVerb: t("page.integrations.webhook"),
    chipVariant: "purple",
  },
  telegram: {
    label: t("page.integrations.bot_token"),
    configureVerb: t("page.integrations.telegram_bot"),
    chipVariant: "blue",
  },
};

// ──────────────────────────────────────────────────────────────────────
// Google Workspace consolidation
// ──────────────────────────────────────────────────────────────────────
//
// Gmail / Calendar / Drive share one OAuth client and one Google
// account — three cards is just visual noise. We collapse them into a
// single synthetic row whose ``_googleSubs`` carries the originals;
// GoogleWorkspaceCard renders one card with three internal Connect
// rows.

function buildGoogleGroupRow(subs: McpServerRow[]): McpServerRow {
  const anyConnected = subs.some(
    (s) => s.connections.length > 0 || (s.entity_accounts?.length ?? 0) > 0,
  );
  const allConfigured = subs.every((s) => s.oauth_configured);
  const allReady = subs.every((s) => s.agent_can_use);
  const anyComingSoon = subs.some((s) => s.coming_soon);
  const merged: any = {
    server_key: "_google_workspace",
    name: t("page.integrations.google_workspace"),
    category: t("page.integrations.category_productivity"),
    description: t(
      "page.integrations.sign_in_once_with_your_google_account_gmail_calendar_a",
    ),
    tagline: t("page.integrations.gmail_calendar_drive"),
    docs_url: "https://workspace.google.com/",
    setup_hint: null,
    color_hex: "#4285F4",
    auth_type: "oauth2",
    scopes: null,
    supports_multi_account: false,
    connections: subs.flatMap((s) => s.connections),
    entity_accounts: subs.flatMap((s) => s.entity_accounts ?? []),
    user_connected: anyConnected,
    user_expires_at: null,
    entity_connected: anyConnected,
    required_permission: null,
    user_has_required_permission: true,
    agent_can_use: allReady,
    hint: anyComingSoon ? t("page.integrations.coming_soon") : "",
    coming_soon: anyComingSoon,
    nango_provider_config_key: null,
    oauth_configured: allConfigured,
    _googleSubs: subs,
  };
  return merged as McpServerRow;
}

function connectionLabel(
  account:
    | {
        name?: string | null;
        display_name?: string | null;
        provider_user_id?: string | null;
      }
    | null
    | undefined,
  fallback: string,
) {
  return (
    (
      account?.name ||
      account?.display_name ||
      account?.provider_user_id ||
      ""
    ).trim() || fallback
  );
}

function providerAccountFallback(serverKey: string) {
  const providerName =
    _CHANNEL_LABEL[serverKey] ||
    serverKey
      .split("_")
      .filter(Boolean)
      .map((part) => part[0]?.toUpperCase() + part.slice(1))
      .join(" ");
  return t("page.integrations.provider_account_fallback").replace(
    "{provider}",
    providerName,
  );
}


function GoogleWorkspaceCard({
  subs,
  canManage,
  onConnect,
  onConfigureOAuth,
}: {
  subs: McpServerRow[];
  canManage: boolean;
  onConnect: (serverKey: string, name: string) => void;
  onConfigureOAuth: (
    serverKey: string,
    name: string,
    scopes?: string | null,
  ) => void;
}) {
  const logoColor = getLogoColor("google_calendar", "#4285F4");
  const isComingSoon = subs.some((s) => s.coming_soon);
  const connectedCount = subs.filter(
    (s) => s.connections.length > 0 || (s.entity_accounts?.length ?? 0) > 0,
  ).length;
  const total = subs.length;
  const hasAnyConnection = connectedCount > 0;
  const statusColor = hasAnyConnection ? "#168a5b" : "#d6d3d1";
  const statusLabel = isComingSoon
    ? t("page.integrations.coming_soon")
    : connectedCount === total
      ? t("page.integrations.ready")
      : connectedCount > 0
        ? t("page.integrations.connected_count_of_total")
            .replace("{count}", String(connectedCount))
            .replace("{total}", String(total))
        : t("page.integrations.not_connected");

  return (
    <CompactCard
      icon={
        <IconTile color={logoColor} size={34}>
          <span style={{ fontWeight: 700, fontSize: 13, color: logoColor }}>
            {t("page.integrations.g")}
          </span>
        </IconTile>
      }
      title={t("page.integrations.google_workspace")}
      subtitle={t(
        "page.integrations.gmail_calendar_drive_one_google_sign_in_three_se",
      )}
      meta={
        <span
          title={statusLabel}
          style={{
            width: 7,
            height: 7,
            borderRadius: "50%",
            background: "currentColor",
          }}
        />
      }
      metaTone={hasAnyConnection ? "connected" : "muted"}
      onClick={() =>
        openDetail({
          icon: (
            <IconTile color={logoColor} size={48}>
              <span style={{ fontWeight: 700, fontSize: 18, color: logoColor }}>
                {t("page.integrations.g")}
              </span>
            </IconTile>
          ),
          title: t("page.integrations.google_workspace"),
          subtitle: t(
            "page.integrations.gmail_calendar_drive_one_google_sign_in_three_se",
          ),
          badges: (
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 5,
                fontSize: 10.5,
                fontWeight: 600,
                color: "#57534e",
                background: "#f5f5f4",
                padding: "2px 9px",
                borderRadius: 6,
              }}
            >
              <span
                style={{
                  width: 6,
                  height: 6,
                  borderRadius: "50%",
                  background: statusColor,
                }}
              />
              {statusLabel}
            </span>
          ),
          body: (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {subs.map((s) => {
                const hasConn =
                  s.connections.length > 0 ||
                  (s.entity_accounts?.length ?? 0) > 0;
                const Icon = MCP_ICON[s.server_key];
                const friendlyName =
                  s.server_key === "gmail"
                    ? t("page.integrations.gmail")
                    : s.server_key === "google_calendar"
                      ? t("page.integrations.calendar")
                      : s.server_key === "google_drive"
                        ? t("page.integrations.drive")
                        : s.name;
                return (
                  <div
                    key={s.server_key}
                    style={{
                      display: "flex",
                      flexDirection: "column",
                      gap: 8,
                      padding: "8px 10px",
                      borderRadius: 8,
                      background: "#f7f6f4",
                      opacity: isComingSoon ? 0.6 : 1,
                    }}
                  >
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 10,
                      }}
                    >
                      <div
                        style={{
                          width: 22,
                          height: 22,
                          borderRadius: 6,
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                          background: "#ffffff",
                          color: getLogoColor(s.server_key),
                          flexShrink: 0,
                        }}
                      >
                        <IntegrationLogo
                          serverKey={s.server_key}
                          size={14}
                          fallback={
                            Icon ? (
                              <Icon size={14} />
                            ) : (
                              <span style={{ fontSize: 11, fontWeight: 700 }}>
                                {friendlyName[0]}
                              </span>
                            )
                          }
                        />
                      </div>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div
                          style={{
                            fontSize: 12,
                            fontWeight: 600,
                            color: "#1c1917",
                          }}
                        >
                          {friendlyName}
                        </div>
                        <div style={{ fontSize: 10, color: "#a8a29e" }}>
                          {isComingSoon
                            ? t("page.integrations.coming_soon")
                            : hasConn
                              ? t("status.connected")
                              : s.oauth_configured
                                ? t("page.integrations.ready_to_connect")
                                : t("page.integrations.oauth_not_configured")}
                        </div>
                      </div>
                      {isComingSoon ? (
                        <Chip size="sm" variant="slate">
                          {t("page.integrations.soon")}
                        </Chip>
                      ) : hasConn ? (
                        <Chip size="sm" variant="slate">
                          ✓
                        </Chip>
                      ) : s.oauth_configured ? (
                        <Button
                          variant="primary"
                          size="sm"
                          onClick={() => onConnect(s.server_key, s.name)}
                        >
                          {t("page.apps.connect")}
                        </Button>
                      ) : canManage ? (
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => {
                            closeDetail();
                            onConfigureOAuth(s.server_key, s.name, s.scopes);
                          }}
                        >
                          {t("page.integrations.configure_client")}
                        </Button>
                      ) : null}
                    </div>
                    {hasConn && (
                      <div
                        style={{
                          display: "flex",
                          flexDirection: "column",
                          gap: 6,
                          paddingLeft: 32,
                        }}
                      >
                        {s.connections.map((connection) => (
                          <ConnectionRow
                            key={`google-oauth-${s.server_key}-${connection.id}`}
                            connection={connection}
                            serverKey={s.server_key}
                            showActions={canManage}
                            onReconnect={() => onConnect(s.server_key, s.name)}
                          />
                        ))}
                        {(s.entity_accounts ?? []).map((account) => (
                          <EntityAccountRow
                            key={`google-entity-${s.server_key}-${account.id}`}
                            account={account}
                            serverKey={s.server_key}
                            onEdit={() => {}}
                            showActions={false}
                          />
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          ),
        })
      }
    />
  );
}


function ServerCard({
  server,
  canManage,
  onConnect,
  onConfigureOAuth,
  onAddApiKey,
  onEditAccount,
  onScanQr,
}: {
  server: McpServerRow;
  canManage: boolean;
  onConnect: () => void;
  onConfigureOAuth?: () => void;
  onAddApiKey: () => void;
  onEditAccount: (accountId: string) => void;
  onScanQr?: (accountId: string) => void;
}) {
  const logoColor = getLogoColor(server.server_key);
  const brandColor = logoColor;

  const isOAuth = server.auth_type === "oauth2";
  const isCredentials =
    server.auth_type === "credentials" || server.auth_type === "api_key";
  let isCliWorker = false;
  let isLocalWorkerManager = false;
  let isManagedSessionCard = false;
  let isChromeManagedSession = false;
  const hasConnections = server.connections.length > 0;
  const hasEntityAccounts = (server.entity_accounts?.length ?? 0) > 0;
  const hasPartialConnection =
    hasConnections ||
    hasEntityAccounts ||
    (isManagedSessionCard && server.user_connected);
  const isReadyConnection = isManagedSessionCard
    ? server.agent_can_use
    : server.agent_can_use || hasPartialConnection;
  const genericCapabilities = isManagedSessionCard
    ? []
    : server.capabilities || [];
  const showGenericCapabilities = genericCapabilities.length > 0;
  let showManagedSessionCapabilities = false;
  let managedSessionNeedsLoginSave = false;
  let managedSessionBusy = false;
  const isEntityLevel = !!server.required_permission;
  const Icon = MCP_ICON[server.server_key];
  const monogram =
    server.name
      .replace(/[^A-Za-z]/g, "")
      .slice(0, 2)
      .toUpperCase() || "?";

  // "Coming soon" — driven by the backend API response (single source of truth).
  // Cards still render so users see what's coming, but the action
  // button is disabled with a "Coming soon" label.
  const isComingSoon = !!server.coming_soon;
  const defaultEntityAccount =
    server.entity_accounts.find((a) => a.is_default) ||
    server.entity_accounts[0];
  const allAccountCount =
    server.connections.length + server.entity_accounts.length;

  // Per-provider auth label (email vs WeChat vs generic credentials/api_key).
  const authEntry = AUTH_LABELS[server.server_key];
  const configureNoun =
    authEntry?.configureVerb || t("page.integrations.credentials");

  // Status indicator: green when connected/ready, gray when disconnected.
  const statusColor = isReadyConnection ? "#168a5b" : "#d6d3d1";
  const statusLabel = server.agent_can_use
    ? t("page.integrations.ready")
    : hasPartialConnection
      ? t("page.integrations.needs_attention")
      : t("page.integrations.not_connected");
  const detailKey = `integration:${server.server_key}`;
  const currentDetailKey = useDetailStore((s) => s.payload?.key);

  function openIntegrationDetail() {
    openDetail({
      key: detailKey,
      icon: (
        <IconTile color={brandColor} size={48}>
          <IntegrationLogo
            serverKey={server.server_key}
            size={24}
            fallback={
              Icon ? (
                <Icon size={24} style={{ color: logoColor }} />
              ) : (
                <span style={{ color: logoColor, fontWeight: 800 }}>
                  {monogram}
                </span>
              )
            }
          />
        </IconTile>
      ),
      title: server.name,
      subtitle: server.category || server.tagline || undefined,
      badges: (
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 5,
            fontSize: 10.5,
            fontWeight: 600,
            color: "#57534e",
            background: "#f5f5f4",
            padding: "2px 9px",
            borderRadius: 6,
          }}
        >
          <span
            style={{
              width: 6,
              height: 6,
              borderRadius: "50%",
              background: statusColor,
            }}
          />
          {statusLabel}
        </span>
      ),
      body: (
        <>
          <p style={{ margin: 0, color: "#44403c" }}>
            {server.description ||
              server.tagline ||
              server.setup_hint ||
              t(
                "page.integrations.connect_this_integration_then_use_its_tools_from_any_b",
              )}
          </p>
          {showGenericCapabilities && (
            <>
              <div
                style={{
                  fontSize: 10,
                  fontWeight: 700,
                  color: "#a8a29e",
                  textTransform: "uppercase",
                  letterSpacing: "0.06em",
                  margin: "16px 0 8px",
                }}
              >
                What it can do
              </div>
              <ul
                style={{
                  margin: 0,
                  paddingLeft: 18,
                  color: "#57534e",
                  fontSize: 12.5,
                  lineHeight: 1.7,
                }}
              >
                {genericCapabilities.slice(0, 6).map((cap, i) => (
                  <li key={i}>{cap}</li>
                ))}
              </ul>
            </>
          )}
          {allAccountCount > 0 && (
            <>
              <div
                style={{
                  fontSize: 10,
                  fontWeight: 700,
                  color: "#a8a29e",
                  textTransform: "uppercase",
                  letterSpacing: "0.06em",
                  margin: "16px 0 8px",
                }}
              >
                Connected accounts
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {server.connections.map((c) => (
                  <ConnectionRow
                    key={`oauth-${c.id}`}
                    connection={c}
                    serverKey={server.server_key}
                    showActions={canManage}
                    onReconnect={isOAuth ? onConnect : undefined}
                  />
                ))}
                {server.entity_accounts.map((account) => (
                  <EntityAccountRow
                    key={`entity-${account.id}`}
                    account={account}
                    serverKey={server.server_key}
                    onEdit={() => onEditAccount(account.id)}
                    onScanQr={onScanQr}
                    showActions={canManage}
                  />
                ))}
              </div>
            </>
          )}
        </>
      ),
      actions: (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 8,
            width: "100%",
          }}
        >
          {isComingSoon ? (
            <Button variant="outline" disabled style={{ width: "100%" }}>
              {t("page.integrations.coming_soon")}
            </Button>
          ) : server.nango_provider_config_key && !server.entity_connected ? (
            <NangoConnectButton
              providerConfigKeys={[server.nango_provider_config_key]}
              label={t("page.apps.connect")}
              variant="primary"
              size="sm"
            />
          ) : server.nango_provider_config_key &&
            server.entity_connected &&
            server.supports_multi_account ? (
            <NangoConnectButton
              providerConfigKeys={[server.nango_provider_config_key]}
              label={t("page.integrations.plus_add_account")}
              variant="outline"
              size="sm"
            />
          ) :
          isCredentials ? (
            <Button
              variant={hasEntityAccounts ? "outline" : "primary"}
              style={{ width: "100%" }}
              onClick={() => {
                closeDetail();
                onAddApiKey();
              }}
            >
              {hasEntityAccounts && server.supports_multi_account
                ? t("page.integrations.plus_add_account")
                : configureNoun}
            </Button>
          ) : isOAuth && server.oauth_configured && !hasConnections ? (
            <Button
              variant="primary"
              style={{ width: "100%" }}
              onClick={() => {
                closeDetail();
                onConnect();
              }}
            >
              {t("page.apps.connect")}
            </Button>
          ) : isOAuth && hasConnections && server.supports_multi_account ? (
            <Button
              variant="outline"
              style={{ width: "100%" }}
              onClick={() => {
                closeDetail();
                onConnect();
              }}
            >
              {t("page.integrations.plus_add_account")}
            </Button>
          ) : isOAuth ? (
            canManage && onConfigureOAuth ? (
              <Button
                variant="primary"
                style={{ width: "100%" }}
                onClick={() => {
                  closeDetail();
                  onConfigureOAuth();
                }}
              >
                {t("page.integrations.configure_client")}
              </Button>
            ) : (
              <Chip variant="slate" size="sm">
                {t("page.integrations.oauth_not_configured")}
              </Chip>
            )
          ) : null}
        </div>
      ),
    });
  }

  useEffect(() => {
    if (currentDetailKey !== detailKey) return;
    openIntegrationDetail();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    currentDetailKey,
    detailKey,
    server.agent_can_use,
    server.category,
    server.description,
    server.entity_connected,
    server.user_connected,
    server.setup_hint,
    server.tagline,
  ]);

  return (
    <CompactCard
      icon={
        <IconTile color={brandColor} size={34}>
          <IntegrationLogo
            serverKey={server.server_key}
            size={18}
            fallback={
              Icon ? (
                <Icon size={18} style={{ color: logoColor }} />
              ) : (
                <span style={{ color: logoColor }}>{monogram}</span>
              )
            }
          />
        </IconTile>
      }
      title={server.name}
      subtitle={server.tagline || server.category || ""}
      meta={
        <span
          title={statusLabel}
          style={{
            width: 7,
            height: 7,
            borderRadius: "50%",
            background: "currentColor",
          }}
        />
      }
      metaTone={isReadyConnection ? "connected" : "muted"}
      onClick={openIntegrationDetail}
    />
  );
}


/* ══════════════════════════════════════════════════════════════════════════
   EmailConfigModal — entity-level IMAP + SMTP credentials. One bundle
   covers both inbox (read/flag/move) and outbox (send). Stored in the
   ``integrations`` table as provider="email".
   ══════════════════════════════════════════════════════════════════════════ */

type EmailPreset = {
  label: string;
  imap_host: string;
  imap_port: number;
  smtp_host: string;
  smtp_port: number;
  hint?: string;
};

const EMAIL_PRESETS: EmailPreset[] = [
  {
    label: t("page.integrations.gmail"),
    imap_host: "imap.gmail.com",
    imap_port: 993,
    smtp_host: "smtp.gmail.com",
    smtp_port: 587,
    hint: t("page.integrations.gmail_app_password_hint"),
  },
  {
    label: t("page.integrations.outlook"),
    imap_host: "outlook.office365.com",
    imap_port: 993,
    smtp_host: "smtp.office365.com",
    smtp_port: 587,
  },
  {
    label: t("page.integrations.icloud"),
    imap_host: "imap.mail.me.com",
    imap_port: 993,
    smtp_host: "smtp.mail.me.com",
    smtp_port: 587,
    hint: t("page.integrations.icloud_app_specific_password_hint"),
  },
  {
    label: t("page.integrations.yahoo"),
    imap_host: "imap.mail.yahoo.com",
    imap_port: 993,
    smtp_host: "smtp.mail.yahoo.com",
    smtp_port: 587,
  },
  {
    label: t("page.integrations.fastmail"),
    imap_host: "imap.fastmail.com",
    imap_port: 993,
    smtp_host: "smtp.fastmail.com",
    smtp_port: 587,
  },
  {
    label: t("page.integrations.sendgrid"),
    imap_host: "",
    imap_port: 993,
    smtp_host: "smtp.sendgrid.net",
    smtp_port: 587,
    hint: t("page.integrations.sendgrid_send_only_hint"),
  },
];

function EmailConfigModal({
  open,
  serverKey,
  accountId,
  onClose,
}: {
  open: boolean;
  serverKey: string | null;
  /** When set, the modal edits this Integration row; else it creates a new one. */
  accountId?: string | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const [accountName, setAccountName] = useState("");
  const [imapHost, setImapHost] = useState("");
  const [imapPort, setImapPort] = useState("993");
  const [smtpHost, setSmtpHost] = useState("");
  const [smtpPort, setSmtpPort] = useState("587");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [fromAddress, setFromAddress] = useState("");
  const [presetHint, setPresetHint] = useState<string | null>(null);
  const editing = !!accountId;

  const { data: existingRow } = useQuery({
    queryKey: ["email-integration-row", accountId],
    enabled: editing && !!accountId,
    queryFn: async () => {
      const list = await api.integrations.list();
      return (list as any[]).find((i) => i.id === accountId) || null;
    },
  });
  // Pre-fill fields once when the existing row lands. useEffect avoids
  // infinite re-render loops from setState-during-render.
  // Secret fields come back in credential_preview as the _SECRET_MASK
  // sentinel; we show them as empty and re-submit the sentinel on save
  // so the backend keeps the stored value.
  const [passwordTouched, setPasswordTouched] = useState(false);
  useEffect(() => {
    if (!editing || !existingRow) return;
    const c = existingRow.credential_preview || {};
    const cfg = existingRow.config || {};
    setImapHost(String(c.imap_host || ""));
    setImapPort(String(c.imap_port || "993"));
    setSmtpHost(String(c.smtp_host || ""));
    setSmtpPort(String(c.smtp_port || "587"));
    setUsername(String(c.username || ""));
    setPassword(c.password === UNCHANGED ? "" : String(c.password || ""));
    setFromAddress(String(c.from_address || ""));
    setAccountName(String(cfg.name || ""));
    setPasswordTouched(false);
  }, [editing, existingRow]);

  const mutation = useMutation({
    mutationFn: () => {
      const credentials: Record<string, unknown> = {
        imap_host: imapHost,
        imap_port: Number(imapPort) || 993,
        smtp_host: smtpHost,
        smtp_port: Number(smtpPort) || 587,
        username,
        // Preserve existing password when editing and the user didn't
        // type anything new.
        password: editing && !passwordTouched ? UNCHANGED : password,
        from_address: fromAddress || username,
        use_ssl_imap: Number(imapPort) === 993,
        use_tls_smtp: Number(smtpPort) === 587,
        use_ssl_smtp: Number(smtpPort) === 465,
      };
      const config: Record<string, unknown> = {
        from_address: fromAddress || username,
      };
      if (accountName.trim()) config.name = accountName.trim();

      if (editing && accountId) {
        return api.integrations.update(accountId, { credentials, config });
      }
      return api.integrations.create({
        provider: serverKey!,
        config,
        credentials,
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["mcp-servers"] });
      toast.success(
        editing
          ? t("page.integrations.email_account_updated")
          : t("page.integrations.email_account_added"),
      );
      reset();
      onClose();
    },
    onError: (e: any) => {
      toast.error(
        t("page.integrations.failed_to_save_email_account"),
        e?.message || t("page.integrations.unknown_error"),
      );
    },
  });

  function reset() {
    setAccountName("");
    setImapHost("");
    setImapPort("993");
    setSmtpHost("");
    setSmtpPort("587");
    setUsername("");
    setPassword("");
    setFromAddress("");
    setPresetHint(null);
  }

  function applyPreset(p: EmailPreset) {
    setImapHost(p.imap_host);
    setImapPort(String(p.imap_port));
    setSmtpHost(p.smtp_host);
    setSmtpPort(String(p.smtp_port));
    setPresetHint(p.hint || null);
  }

  if (!serverKey) return null;

  // When editing, password may stay empty (we send UNCHANGED sentinel).
  const canSave =
    smtpHost && username && (editing || password) && !mutation.isPending;

  return (
    <Modal
      open={open}
      onClose={() => {
        reset();
        onClose();
      }}
      title={
        editing
          ? t("page.integrations.edit_email_account")
          : t("page.integrations.add_email_account")
      }
      footer={
        <>
          <Button
            variant="outline"
            onClick={() => {
              reset();
              onClose();
            }}
          >
            {t("action.cancel")}
          </Button>
          <Button
            variant="primary"
            disabled={!canSave}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? t("page.agents.saving") : t("action.save")}
          </Button>
        </>
      }
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <p
          style={{ fontSize: 12, color: "#78716c", lineHeight: 1.5, margin: 0 }}
        >
          {t(
            "page.integrations.one_credential_bundle_for_reading_imap_and_sendi",
          )}{" "}
          <code style={{ margin: "0 4px" }}>
            {t("page.integrations.integrations_manage")}
          </code>
          {t(
            "page.integrations.can_operate_this_account_on_the_entity_s_behalf",
          )}
        </p>

        <Input
          label={t("page.integrations.account_name_optional")}
          value={accountName}
          onChange={(e) => setAccountName(e.target.value)}
          placeholder={t("page.integrations.e_g_support_or_sales")}
        />

        {/* Quick presets */}
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {EMAIL_PRESETS.map((p) => (
            <Chip key={p.label} variant="slate" onClick={() => applyPreset(p)}>
              {p.label}
            </Chip>
          ))}
        </div>

        {presetHint && <InfoAlert tone="amber">{presetHint}</InfoAlert>}

        {/* IMAP */}
        <div>
          <div
            style={{
              fontSize: 11,
              fontWeight: 800,
              letterSpacing: "0.08em",
              textTransform: "uppercase" as const,
              color: "#78716c",
              marginBottom: 6,
            }}
          >
            {t("page.integrations.inbox_imap")}
          </div>
          <div
            style={{
              display: "grid",
              gridTemplateColumns:
                "repeat(auto-fit, minmax(min(100%, 280px), 1fr))",
              gap: 10,
            }}
          >
            <Input
              label={t("page.integrations.imap_host")}
              value={imapHost}
              onChange={(e) => setImapHost(e.target.value)}
              placeholder={t(
                "page.integrations.imap_gmail_com_leave_blank_for_send_only",
              )}
            />
            <Input
              label={t("page.integrations.port")}
              value={imapPort}
              onChange={(e) => setImapPort(e.target.value)}
              placeholder="993"
            />
          </div>
        </div>

        {/* SMTP */}
        <div>
          <div
            style={{
              fontSize: 11,
              fontWeight: 800,
              letterSpacing: "0.08em",
              textTransform: "uppercase" as const,
              color: "#78716c",
              marginBottom: 6,
            }}
          >
            {t("page.integrations.outbox_smtp")}
          </div>
          <div
            style={{
              display: "grid",
              gridTemplateColumns:
                "repeat(auto-fit, minmax(min(100%, 280px), 1fr))",
              gap: 10,
            }}
          >
            <Input
              label={t("page.integrations.smtp_host")}
              value={smtpHost}
              onChange={(e) => setSmtpHost(e.target.value)}
              placeholder={t("page.integrations.smtp_gmail_com")}
            />
            <Input
              label={t("page.integrations.port")}
              value={smtpPort}
              onChange={(e) => setSmtpPort(e.target.value)}
              placeholder="587"
            />
          </div>
        </div>

        {/* Credentials */}
        <Input
          label={t("page.integrations.username")}
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          placeholder={t("page.integrations.user_example_com")}
        />
        <Input
          label={t("page.team_people.password")}
          type="password"
          value={password}
          onChange={(e) => {
            setPassword(e.target.value);
            setPasswordTouched(true);
          }}
          placeholder={
            editing
              ? t("page.integrations.leave_blank_to_keep_existing")
              : t("page.integrations.app_password_or_api_key")
          }
        />
        <Input
          label={t("page.integrations.from_address_optional")}
          value={fromAddress}
          onChange={(e) => setFromAddress(e.target.value)}
          placeholder={t("page.integrations.defaults_to_username")}
        />

        {mutation.isError && (
          <p style={{ color: "#c14a44", fontSize: 12 }}>
            {(mutation.error as Error).message}
          </p>
        )}
      </div>
    </Modal>
  );
}

/* ══════════════════════════════════════════════════════════════════════════
   ApiKeyConfigModal — data-driven credential form for providers that use
   an API key / bearer token. One row per field, shape declared by
   ``API_KEY_FIELDS``. On save it upserts into the ``integrations`` table
   as provider=<server_key>; agents read that row at call time.
   ══════════════════════════════════════════════════════════════════════════ */

type ApiKeyField = {
  key: string;
  label: string;
  type?: "text" | "password" | "textarea";
  placeholder?: string;
  required?: boolean;
  help?: string;
  rows?: number;
  /** Pre-fill on first open. Useful for fields the user shouldn't have
   *  to type for the default Manor deployment (e.g. the in-cluster
   *  Docker hostname of a sidecar runner). */
  default_value?: string;
};

type ApiKeyProviderSpec = {
  fields: ApiKeyField[];
  docs_hint?: string;
};

const API_KEY_FIELDS: Record<string, ApiKeyProviderSpec> = {
  stripe: {
    fields: [
      {
        key: "secret_key",
        label: t("page.integrations.secret_key"),
        type: "password",
        placeholder: "sk_live_…",
        required: true,
      },
      {
        key: "webhook_secret",
        label: t("page.integrations.webhook_secret_optional"),
        type: "password",
        placeholder: "whsec_…",
      },
    ],
    docs_hint: t("page.integrations.docs_hint_01"),
  },
  twilio: {
    fields: [
      {
        key: "account_sid",
        label: t("page.integrations.account_sid"),
        placeholder: "AC…",
        required: true,
      },
      {
        key: "auth_token",
        label: t("page.integrations.auth_token"),
        type: "password",
        required: true,
      },
      {
        key: "phone_number",
        label: t("page.integrations.default_from_number"),
        placeholder: "+14155552671",
      },
    ],
    docs_hint: t("page.integrations.docs_hint_02"),
  },
  whatsapp: {
    fields: [
      {
        key: "api_key",
        label: t("page.api_keys.api_key"),
        type: "password",
        placeholder: t(
          "page.integrations.your_twilio_auth_token_or_provider_api_key",
        ),
        required: true,
      },
      {
        key: "phone_id",
        label: t("page.integrations.phone_number_sender_id"),
        placeholder: t("page.integrations.whatsapp_14155238886"),
        required: true,
      },
    ],
    docs_hint: t("page.integrations.docs_hint_03"),
  },
  discord: {
    fields: [
      {
        key: "bot_token",
        label: t("page.integrations.bot_token"),
        type: "password",
        placeholder: t("page.integrations.mtq3"),
        required: true,
      },
      {
        key: "default_guild_id",
        label: t("page.integrations.default_server_guild_id_optional"),
      },
    ],
    docs_hint: t("page.integrations.docs_hint_04"),
  },
  telegram: {
    fields: [
      {
        key: "bot_token",
        label: t("page.integrations.bot_token"),
        type: "password",
        placeholder: "123456:ABC-DEF…",
        required: true,
      },
      {
        key: "default_chat_id",
        label: t("page.integrations.default_chat_id_optional"),
        placeholder: "-100…",
      },
    ],
    docs_hint: t("page.integrations.docs_hint_05"),
  },
  wechat_personal: {
    fields: [
      {
        key: "runner_url",
        label: t("page.integrations.bot_runner_url"),
        required: true,
        default_value: "http://wechat-runner:8800",
        placeholder: "http://wechat-runner:8800",
        help: t("page.integrations.field_help_06"),
      },
      {
        key: "bearer_token",
        label: t("page.integrations.runner_bearer_token_optional"),
        type: "password",
        placeholder: t(
          "page.integrations.shared_secret_must_match_runner_bearer_token_on_the_si",
        ),
        help: t("page.integrations.field_help_07"),
      },
      {
        key: "default_target",
        label: t("page.integrations.default_ilink_user_id_optional"),
        placeholder: t(
          "page.integrations.cached_automatically_when_a_contact_messages_you_first",
        ),
      },
    ],
    docs_hint: t("page.integrations.docs_hint_08"),
  },
  wechat_official: {
    fields: [
      {
        key: "app_id",
        label: t("page.integrations.appid"),
        placeholder: t("page.integrations.wx"),
        required: true,
      },
      {
        key: "app_secret",
        label: t("page.integrations.appsecret"),
        type: "password",
        required: true,
      },
      {
        key: "token",
        label: t("page.integrations.callback_token"),
        placeholder: t("page.integrations.token_set_in_the_oa_admin_panel"),
        required: true,
      },
      {
        key: "encoding_aes_key",
        label: t("page.integrations.encodingaeskey_optional"),
        type: "password",
        placeholder: t(
          "page.integrations.only_if_using_encrypted_message_mode",
        ),
      },
    ],
    docs_hint: t("page.integrations.docs_hint_09"),
  },
  webhook: {
    fields: [
      {
        key: "url",
        label: t("page.integrations.webhook_url"),
        placeholder: "https://example.com/webhook",
        required: true,
      },
      {
        key: "bearer_token",
        label: t("page.integrations.bearer_token_optional"),
        type: "password",
        placeholder: t("page.integrations.sent_as_authorization_header"),
      },
    ],
    docs_hint: t("page.integrations.docs_hint_10"),
  },
  replicate: {
    fields: [
      {
        key: "api_key",
        label: t("page.integrations.api_token"),
        type: "password",
        placeholder: t("page.integrations.r8"),
        required: true,
        help: t("page.integrations.field_help_11"),
      },
    ],
    docs_hint: t("page.integrations.docs_hint_12"),
  },
  elevenlabs: {
    fields: [
      {
        key: "api_key",
        label: t("page.api_keys.api_key"),
        type: "password",
        placeholder: t("page.integrations.your_xi_api_key"),
        required: true,
        help: t("page.integrations.field_help_13"),
      },
    ],
    docs_hint: t("page.integrations.docs_hint_14"),
  },
  tavily: {
    fields: [
      {
        key: "api_key",
        label: t("page.api_keys.api_key"),
        type: "password",
        placeholder: t("page.integrations.tvly"),
        required: true,
        help: t("page.integrations.field_help_15"),
      },
    ],
    docs_hint: t("page.integrations.docs_hint_16"),
  },
  jimeng: {
    fields: [
      {
        key: "api_key",
        label: t("page.browser_sessions.session_id"),
        type: "password",
        placeholder: t(
          "page.integrations.paste_your_jimeng_jianying_com_sessionid_cookie",
        ),
        required: true,
        help: t("page.integrations.field_help_17"),
      },
    ],
    docs_hint: t("page.integrations.docs_hint_18"),
  },
  notebooklm: {
    fields: [
      {
        key: "api_key",
        label: t("page.integrations.cookie_jar_json"),
        type: "textarea",
        placeholder: t("page.integrations.name_nid_value_domain_google_com"),
        required: true,
        rows: 8,
        help: t("page.integrations.field_help_19"),
      },
    ],
    docs_hint: t("page.integrations.docs_hint_20"),
  },
  claude_ai_web: {
    fields: [
      {
        key: "api_key",
        label: t("page.integrations.cookie_jar_json"),
        type: "textarea",
        placeholder: t(
          "page.integrations.name_sessionkey_value_domain_claude_ai",
        ),
        required: true,
        rows: 8,
        help: t("page.integrations.field_help_21"),
      },
    ],
    docs_hint: t("page.integrations.docs_hint_22"),
  },
  chatgpt_web: {
    fields: [
      {
        key: "api_key",
        label: t("page.integrations.cookie_jar_json"),
        type: "textarea",
        placeholder: t(
          "page.integrations.name_secure_next_auth_session_token_value_domain_chatg",
        ),
        required: true,
        rows: 8,
        help: t("page.integrations.field_help_23"),
      },
    ],
    docs_hint: t("page.integrations.docs_hint_24"),
  },
  gemini_web: {
    fields: [
      {
        key: "api_key",
        label: t("page.integrations.cookie_jar_json"),
        type: "textarea",
        placeholder: t(
          "page.integrations.name_secure_1psid_value_domain_google_com",
        ),
        required: true,
        rows: 8,
        help: t("page.integrations.field_help_25"),
      },
    ],
    docs_hint: t("page.integrations.docs_hint_26"),
  },
  perplexity_web: {
    fields: [
      {
        key: "api_key",
        label: t("page.integrations.cookie_jar_json"),
        type: "textarea",
        placeholder: t(
          "page.integrations.name_secure_next_auth_session_token_value_domain_perpl",
        ),
        required: true,
        rows: 8,
        help: t("page.integrations.field_help_27"),
      },
    ],
    docs_hint: t("page.integrations.docs_hint_28"),
  },
};

function OAuthClientConfigModal({
  open,
  target,
  onClose,
}: {
  open: boolean;
  target: { key: string; name: string; scopes?: string | null } | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [scopes, setScopes] = useState("");

  useEffect(() => {
    if (!open || !target) return;
    setClientId("");
    setClientSecret("");
    setScopes(target.scopes || "");
  }, [open, target?.key, target?.scopes]);

  const mutation = useMutation({
    mutationFn: () => {
      if (!target) return Promise.resolve();
      return api.integrations.setOAuthConfig(target.key, {
        client_id: clientId.trim(),
        client_secret: clientSecret.trim(),
        scopes: scopes.trim() || undefined,
      });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["mcp-servers"] });
      toast.success(
        t("page.integrations.oauth_client_saved").replace(
          "{name}",
          target?.name || "OAuth",
        ),
      );
      setClientId("");
      setClientSecret("");
      onClose();
    },
    onError: (e: any) => {
      toast.error(
        t("page.integrations.oauth_client_save_failed"),
        e?.message || t("page.integrations.unknown_error"),
      );
    },
  });

  if (!target) return null;
  const canSave =
    !!clientId.trim() && !!clientSecret.trim() && !mutation.isPending;

  return (
    <Modal
      open={open}
      onClose={() => {
        setClientId("");
        setClientSecret("");
        onClose();
      }}
      title={t("page.integrations.oauth_client_title").replace(
        "{name}",
        target.name,
      )}
      maxWidth="560px"
      footer={
        <>
          <Button
            variant="outline"
            onClick={() => {
              setClientId("");
              setClientSecret("");
              onClose();
            }}
          >
            {t("action.cancel")}
          </Button>
          <Button
            variant="primary"
            disabled={!canSave}
            loading={mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            {t("action.save")}
          </Button>
        </>
      }
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <p
          style={{ fontSize: 12, color: "#78716c", lineHeight: 1.5, margin: 0 }}
        >
          {t("page.integrations.oauth_client_description")}
        </p>

        <Input
          label={t("page.integrations.client_id_or_key")}
          value={clientId}
          onChange={(e) => setClientId(e.target.value)}
          placeholder={t("page.integrations.client_id_or_key_placeholder")}
        />
        <Input
          label={t("page.integrations.client_secret")}
          type="password"
          value={clientSecret}
          onChange={(e) => setClientSecret(e.target.value)}
          placeholder="client_secret"
        />
        <Input
          label={t("page.integrations.scopes_optional")}
          value={scopes}
          onChange={(e) => setScopes(e.target.value)}
          placeholder={target.scopes || t("page.integrations.use_provider_defaults")}
        />

        <InfoAlert>
          {t("page.integrations.oauth_client_saved_hint").replace(
            "{name}",
            target.name,
          )}
        </InfoAlert>
      </div>
    </Modal>
  );
}

function ApiKeyConfigModal({
  open,
  target,
  onClose,
  onSaved,
}: {
  open: boolean;
  /** When `accountId` is set the modal edits that specific Integration row;
   *  otherwise it creates a new one (multi-account). */
  target: { key: string; name: string; accountId?: string } | null;
  onClose: () => void;
  /** Fires after successful save with the resulting integration id +
   *  provider key — used e.g. to pop the WeChat scan panel right after. */
  onSaved?: (integrationId: string, providerKey: string) => void;
}) {
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const spec = target ? API_KEY_FIELDS[target.key] : undefined;
  const editing = !!target?.accountId;

  const [values, setValues] = useState<Record<string, string>>({});
  const [accountName, setAccountName] = useState("");

  // Pre-fill from the existing Integration when editing
  const { data: existingRow } = useQuery({
    queryKey: ["integration-row", target?.accountId],
    enabled: editing && !!target?.accountId,
    queryFn: async () => {
      const list = await api.integrations.list();
      return (list as any[]).find((i) => i.id === target?.accountId) || null;
    },
  });
  // Track which secret fields the user has typed into during edit mode —
  // untouched secret fields resubmit the UNCHANGED sentinel and preserve
  // the stored value.
  const [touched, setTouched] = useState<Record<string, boolean>>({});
  useEffect(() => {
    if (!editing || !existingRow) return;
    const creds = existingRow.credential_preview || {};
    const cfg = existingRow.config || {};
    setValues(
      Object.fromEntries(
        (spec?.fields || []).map((f) => {
          const raw = String(creds[f.key] ?? "");
          // Secret fields come back as the sentinel; show them as empty
          // so the user can type a replacement if they want.
          const isSecret = f.type === "password";
          const display = isSecret && raw === UNCHANGED ? "" : raw;
          return [f.key, display];
        }),
      ),
    );
    setAccountName(String(cfg.name || ""));
    setTouched({});
  }, [editing, existingRow, spec]);

  // Seed default_value into a fresh form on open. Skipped during edit
  // (we already pre-fill from the saved row). Lets per-provider specs
  // ship sensible factory defaults — e.g. the wechat_personal runner
  // URL is fixed at the Docker service hostname for the standard
  // self-hosted Manor deployment.
  useEffect(() => {
    if (editing) return;
    if (!target || !spec) return;
    setValues((prev) => {
      const next = { ...prev };
      let changed = false;
      for (const f of spec.fields) {
        if (
          f.default_value !== undefined &&
          (next[f.key] === undefined || next[f.key] === "")
        ) {
          next[f.key] = f.default_value;
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [target?.key, editing, spec]);

  const mutation = useMutation({
    mutationFn: async () => {
      if (!target) return;
      const credentials: Record<string, unknown> = {};
      for (const f of spec?.fields || []) {
        const v = values[f.key]?.trim();
        const isSecret = f.type === "password";
        if (v) {
          credentials[f.key] = v;
        } else if (editing && isSecret && !touched[f.key]) {
          // Preserve existing secret when editing and user didn't touch it
          credentials[f.key] = UNCHANGED;
        }
      }
      const config: Record<string, unknown> = {};
      if (accountName.trim()) config.name = accountName.trim();

      if (editing && target.accountId) {
        return api.integrations.update(target.accountId, {
          credentials,
          config,
        });
      }
      return api.integrations.create({
        provider: target.key,
        credentials,
        config,
      });
    },
    onSuccess: (resp: any) => {
      queryClient.invalidateQueries({ queryKey: ["mcp-servers"] });
      toast.success(
        editing
          ? t("page.integrations.named_account_updated").replace(
              "{name}",
              target?.name || "",
            )
          : t("page.integrations.named_account_added").replace(
              "{name}",
              target?.name || "",
            ),
      );
      setValues({});
      setAccountName("");
      setTouched({});
      const id = resp?.id || target?.accountId;
      const key = target?.key;
      onClose();
      if (onSaved && id && key) onSaved(id, key);
    },
    onError: (e: any) => {
      toast.error(
        t("page.credential_modal.failed_save"),
        e?.message || t("page.integrations.unknown_error"),
      );
    },
  });

  if (!target || !spec) return null;

  // When editing, untouched secret fields are allowed to be blank —
  // they'll be sent as UNCHANGED and the backend preserves the stored
  // value.
  const missing = spec.fields
    .filter((f) => f.required)
    .some((f) => {
      if (values[f.key]?.trim()) return false;
      if (editing && f.type === "password" && !touched[f.key]) return false;
      return true;
    });
  const canSave = !missing && !mutation.isPending;

  return (
    <Modal
      open={open}
      onClose={() => {
        setValues({});
        onClose();
      }}
      title={(editing
        ? t("page.integrations.edit_named_account")
        : t("page.integrations.add_named_account")
      ).replace("{name}", target.name)}
      footer={
        <>
          <Button
            variant="outline"
            onClick={() => {
              setValues({});
              onClose();
            }}
          >
            {t("action.cancel")}
          </Button>
          <Button
            variant="primary"
            disabled={!canSave}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? t("page.agents.saving") : t("action.save")}
          </Button>
        </>
      }
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <p
          style={{ fontSize: 12, color: "#78716c", lineHeight: 1.5, margin: 0 }}
        >
          {t(
            "page.integrations.credentials_are_stored_at_the_entity_level_any_a",
          )}
        </p>

        <Input
          label={t("page.integrations.account_name_optional")}
          value={accountName}
          onChange={(e) => setAccountName(e.target.value)}
          placeholder={t("page.integrations.e_g_support_inbox_or_sales_bot")}
        />

        {spec.fields.map((f) => {
          const isSecret = f.type === "password";
          const isTextarea = f.type === "textarea";
          const placeholder =
            editing && isSecret
              ? t("page.integrations.leave_blank_to_keep_existing")
              : f.placeholder;
          if (isTextarea) {
            return (
              <div
                key={f.key}
                style={{ display: "flex", flexDirection: "column", gap: 4 }}
              >
                <label
                  style={{ fontSize: 12, fontWeight: 600, color: "#57534e" }}
                >
                  {f.label}
                  {f.required && <span style={{ color: "#c14a44" }}> *</span>}
                </label>
                <textarea
                  value={values[f.key] || ""}
                  onChange={(e) => {
                    setValues((v) => ({ ...v, [f.key]: e.target.value }));
                    setTouched((t) => ({ ...t, [f.key]: true }));
                  }}
                  placeholder={placeholder}
                  rows={f.rows || 6}
                  style={{
                    fontFamily: "ui-monospace, SFMono-Regular, monospace",
                    fontSize: 12,
                    padding: 10,
                    border: "1px solid rgba(28,25,23,0.06)",
                    borderRadius: 8,
                    width: "100%",
                    resize: "vertical",
                  }}
                />
                {f.help && (
                  <span
                    style={{ fontSize: 11, color: "#78716c", lineHeight: 1.4 }}
                  >
                    {f.help}
                  </span>
                )}
              </div>
            );
          }
          return (
            <Input
              key={f.key}
              label={f.label}
              type={f.type || "text"}
              value={values[f.key] || ""}
              onChange={(e) => {
                setValues((v) => ({ ...v, [f.key]: e.target.value }));
                if (isSecret) setTouched((t) => ({ ...t, [f.key]: true }));
              }}
              placeholder={placeholder}
            />
          );
        })}

        {spec.docs_hint && <InfoAlert>{spec.docs_hint}</InfoAlert>}

        {mutation.isError && (
          <p style={{ color: "#c14a44", fontSize: 12 }}>
            {(mutation.error as Error).message}
          </p>
        )}
      </div>
    </Modal>
  );
}

/* ══════════════════════════════════════════════════════════════════════════
   WeChatPersonalScanModal — live QR scan panel for the sidecar runner.
   Polls /status every 8s while open; renders the current QR png while
   ``qr_pending`` is true, swaps to a "Logged in as …" card once the
   runner reports ``online``.
   ══════════════════════════════════════════════════════════════════════════ */

function WeChatPersonalScanModal({
  open,
  accountId,
  onClose,
}: {
  open: boolean;
  /** Either an existing Integration id (re-scan flow) OR a sentinel
   *  ``"new:<ts>"`` for a brand-new connection. */
  accountId: string | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const toast = useToastStore();

  // Distinguish the two flows: a fresh QR (no Integration row yet) vs.
  // re-scanning an existing one. ``new:`` mode owns its own
  // session_id; existing-account mode polls by integration id and
  // proxies to the runner's session under the hood.
  const isNew = !!accountId && accountId.startsWith("new:");

  // Cache-bust key — bumped on every poll so the <img> re-fetches.
  const [tick, setTick] = useState(0);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  const [finishing, setFinishing] = useState(false);

  // Spin up a fresh runner session on open (new flow only). Cleanup
  // tells the runner to drop it if the user bails before finishing.
  useEffect(() => {
    if (!open || !isNew) return;
    let cancelled = false;
    setSessionId(null);
    setStartError(null);
    (async () => {
      try {
        const res = await api.integrations.wechatPersonalStartSession();
        if (cancelled) {
          void api.integrations.wechatPersonalCancelSession(res.session_id);
          return;
        }
        setSessionId(res.session_id);
      } catch (exc: unknown) {
        const msg = exc instanceof Error ? exc.message : String(exc);
        setStartError(msg);
      }
    })();
    return () => {
      cancelled = true;
      // Capture sessionId at cleanup time so a quick close doesn't
      // leak the orphan.
      const sid = sessionId;
      if (sid) void api.integrations.wechatPersonalCancelSession(sid);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, isNew]);

  // Status polling — different endpoint for new vs. existing.
  const { data: status, refetch } = useQuery({
    queryKey: isNew
      ? ["wechat-personal-session-status", sessionId]
      : ["wechat-personal-status", accountId],
    queryFn: () =>
      isNew
        ? api.integrations.wechatPersonalSessionStatus(sessionId!)
        : api.integrations.wechatPersonalStatus(accountId!),
    enabled: open && (isNew ? !!sessionId : !!accountId),
    refetchInterval: open ? 3_000 : false,
    refetchIntervalInBackground: false,
  });

  useEffect(() => {
    if (!open) return;
    const t = setInterval(() => setTick((n) => n + 1), 3_000);
    return () => clearInterval(t);
  }, [open]);

  // The moment the runner reports ``online: true`` for a new session,
  // promote it to a real Integration row and close the modal.
  useEffect(() => {
    if (!isNew || !sessionId || finishing) return;
    if (!status?.online) return;
    setFinishing(true);
    (async () => {
      try {
        const integ =
          await api.integrations.wechatPersonalFinishSession(sessionId);
        toast.success(
          t("page.integrations.wechat_connected"),
          t("page.integrations.account_is_online").replace("{id}", integ.id),
        );
        await queryClient.invalidateQueries({ queryKey: ["mcp-servers"] });
        onClose();
      } catch (exc: unknown) {
        const msg = exc instanceof Error ? exc.message : String(exc);
        toast.error(t("page.integrations.couldn_t_save_wechat_session"), msg);
        setFinishing(false);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isNew, sessionId, status?.online, finishing]);

  const qrUrl = isNew
    ? sessionId
      ? `${api.integrations.wechatPersonalSessionQrUrl(sessionId)}?t=${tick}`
      : null
    : accountId
      ? `${api.integrations.wechatPersonalQrUrl(accountId)}?t=${tick}`
      : null;

  const online = status?.online;
  const qrPending = status?.qr_pending;
  const nick = status?.account?.nick_name || status?.account?.user_name || "";
  const lastError = startError || status?.last_error;
  const waitingForRunner = isNew && !sessionId && !startError;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t("page.integrations.wechat_personal_connection")}
      footer={
        <>
          <Button variant="outline" onClick={() => refetch()}>
            {t("page.simulation_report.refresh")}
          </Button>
          <Button variant="primary" onClick={onClose}>
            {t("common.done")}
          </Button>
        </>
      }
      maxWidth="520px"
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {/* Status / QR area */}
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 14,
            alignItems: "center",
          }}
        >
          {waitingForRunner ? (
            <>
              <LoadingSpinner />
              <div style={{ fontSize: 13, color: "#78716c" }}>
                {t("page.integrations.asking_the_runner_for_a_fresh_session")}
              </div>
            </>
          ) : online ? (
            <>
              <div
                style={{
                  width: 56,
                  height: 56,
                  borderRadius: "50%",
                  background: "#e4efe8",
                  color: "#3d7351",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  fontSize: 24,
                }}
              >
                ✓
              </div>
              <div style={{ textAlign: "center" as const }}>
                <div
                  style={{ fontSize: 14, fontWeight: 700, color: "#1c1917" }}
                >
                  {nick
                    ? t("page.integrations.logged_in_as").replace(
                        "{name}",
                        nick,
                      )
                    : t("page.integrations.logged_in")}
                </div>
                <div style={{ fontSize: 12, color: "#78716c", marginTop: 4 }}>
                  {t(
                    "page.integrations.the_runner_is_online_and_relaying_messages_to_ma",
                  )}
                </div>
              </div>
            </>
          ) : qrUrl && qrPending !== false ? (
            <>
              <div
                style={{
                  padding: 12,
                  border: "1px solid rgba(28,25,23,0.06)",
                  borderRadius: 12,
                  background: "#ffffff",
                }}
              >
                <img
                  src={qrUrl}
                  alt={t("page.integrations.wechat_clawbot_login_qr")}
                  style={{ width: 240, height: 240, display: "block" }}
                  onError={(e) => {
                    (e.currentTarget as HTMLImageElement).style.opacity = "0.3";
                  }}
                />
              </div>
              <div style={{ textAlign: "center" as const, maxWidth: 360 }}>
                <div
                  style={{ fontSize: 13, fontWeight: 600, color: "#1c1917" }}
                >
                  {t("page.integrations.scan_with_the_wechat")}{" "}
                  <span style={{ color: "#07C160" }}>
                    {t("page.integrations.clawbot")}
                  </span>{" "}
                  {t("page.integrations.plugin")}
                </div>
                <div
                  style={{
                    fontSize: 12,
                    color: "#78716c",
                    marginTop: 4,
                    lineHeight: 1.5,
                  }}
                >
                  {t(
                    "page.integrations.open_wechat_on_your_phone_follow_the_steps_below",
                  )}
                </div>
              </div>
            </>
          ) : (
            <>
              <div
                style={{
                  width: 56,
                  height: 56,
                  borderRadius: "50%",
                  background: "#f1dddb",
                  color: "#a23e38",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  fontSize: 24,
                }}
              >
                !
              </div>
              <div style={{ textAlign: "center" as const }}>
                <div
                  style={{ fontSize: 14, fontWeight: 700, color: "#1c1917" }}
                >
                  {t("page.integrations.runner_unreachable")}
                </div>
                <div
                  style={{
                    fontSize: 12,
                    color: "#78716c",
                    marginTop: 4,
                    lineHeight: 1.5,
                  }}
                >
                  {lastError ||
                    t(
                      "page.integrations.no_response_from_the_sidecar_check_runner_url_and_that",
                    )}
                </div>
              </div>
            </>
          )}
        </div>

        {/* Setup steps — visible whenever we're not yet logged in */}
        {!online && (
          <div
            style={{
              alignSelf: "stretch",
              padding: "12px 14px",
              borderRadius: 10,
              background: "#fafaf9",
              border: "1px solid rgba(28,25,23,0.06)",
              display: "flex",
              flexDirection: "column",
              gap: 6,
              fontSize: 12.5,
              color: "#44403c",
              lineHeight: 1.55,
            }}
          >
            <div
              style={{
                fontWeight: 700,
                color: "#1c1917",
                fontSize: 13,
                marginBottom: 2,
              }}
            >
              {t("page.integrations.how_to_scan")}
            </div>
            <div>
              <strong>1.</strong>{" "}
              {t("page.integrations.open_wechat_on_your_phone")}
            </div>
            <div>
              <strong>2.</strong> {t("page.integrations.tap")}{" "}
              <em>{t("page.integrations.clawbot_2")}</em>
            </div>
            <div>
              <strong>3.</strong>{" "}
              {t("page.integrations.on_the_clawbot_page_tap")}{" "}
              <em>{t("page.integrations.connect")}</em>{" "}
              {t("page.integrations.and_scan_the_qr_above")}
            </div>
          </div>
        )}

        {/* Troubleshooting — collapsible-style "Don't see ClawBot?" panel */}
        {!online && (
          <details
            style={{
              alignSelf: "stretch",
              padding: "10px 14px",
              borderRadius: 10,
              background: "#fffaf0",
              border: "1px solid #ecdac2",
              fontSize: 12.5,
              color: "#7c2d12",
              lineHeight: 1.55,
            }}
          >
            <summary
              style={{
                cursor: "pointer",
                fontWeight: 600,
                color: "#7c4a2e",
                listStyle: "none",
                outline: "none",
              }}
            >
              {t("page.integrations.don_t_see_the_clawbot_plugin")}
            </summary>
            <div
              style={{
                marginTop: 8,
                color: "#7c2d12",
                display: "flex",
                flexDirection: "column",
                gap: 5,
              }}
            >
              <div>
                <strong>{t("page.integrations.gray_scale_rollout")}</strong>{" "}
                {t(
                  "page.integrations.tencent_is_releasing_the_plugin_gradually_force",
                )}
              </div>
              <div>
                <strong>{t("page.integrations.ios")}</strong>{" "}
                {t("page.integrations.requires_wechat")} <strong>8.0.70</strong>
                {t("page.integrations.update_from_the_app_store")}
              </div>
              <div>
                <strong>{t("page.integrations.android")}</strong>{" "}
                {t(
                  "page.integrations.users_may_need_early_access_search_for_the",
                )}{" "}
                <em>{t("page.integrations.lobster")}</em>{" "}
                {t(
                  "page.integrations.mini_program_inside_wechat_to_get_the_activation",
                )}
              </div>
              <div>
                <strong>{t("page.integrations.region")}</strong>{" "}
                {t(
                  "page.integrations.currently_rolling_out_to_mainland_china_register",
                )}
              </div>
              <div>
                {t("page.integrations.plugin_path_may_also_appear_under")}{" "}
                <em>{t("page.integrations.text")}</em>{" "}
                {t("page.integrations.on_some_builds")}
              </div>
            </div>
          </details>
        )}

        {/* Wiring + polling status */}
        <div
          style={{
            alignSelf: "stretch",
            padding: "8px 12px",
            borderRadius: 8,
            background: "#fafaf9",
            display: "flex",
            flexDirection: "column",
            gap: 4,
            fontSize: 11.5,
            color: "#57534e",
          }}
        >
          <div>
            <strong>{t("page.integrations.protocol")}</strong>{" "}
            {t("page.integrations.tencent_ilink_bot_api")}
            <code>{t("page.integrations.ilinkai_weixin_qq_com")}</code>)
          </div>
          <div>
            <strong>{t("page.integrations.callback")}</strong>{" "}
            {status?.callback_configured ? (
              <span style={{ color: "#3d7351" }}>
                {t("page.integrations.registered")}
              </span>
            ) : (
              <span style={{ color: "#936027" }}>
                {t(
                  "page.integrations.not_yet_registered_it_s_set_on_credential_save",
                )}
              </span>
            )}
          </div>
          <div>
            <strong>{t("page.integrations.polling")}</strong>{" "}
            {t(
              "page.integrations.status_every_8s_long_poll_cursor_refreshes_on_ea",
            )}
          </div>
        </div>
      </div>
    </Modal>
  );
}

/* ══════════════════════════════════════════════════════════════════════════
   Helpers — display categories + ConnectionRow
   ══════════════════════════════════════════════════════════════════════════ */

function IntegrationSectionHeader({
  categoryKey,
  readyCount,
  totalCount,
}: {
  categoryKey: IntegrationDisplayCategoryKey;
  readyCount: number;
  totalCount: number;
}) {
  const meta = INTEGRATION_DISPLAY_CATEGORIES[categoryKey];
  const Icon = meta.Icon;
  return (
    <div className="integration-section-header">
      <span className="integration-section-icon" aria-hidden="true">
        <Icon size={13} />
      </span>
      <span className="integration-section-title">{t(meta.labelKey)}</span>
      <span className="integration-section-count">
        {readyCount} / {totalCount} {t("page.integrations.ready")}
      </span>
    </div>
  );
}

function integrationDisplayCategory(
  row: McpServerRow,
): IntegrationDisplayCategoryKey {
  const explicit = INTEGRATION_SERVER_CATEGORY_OVERRIDES[row.server_key];
  if (explicit) return explicit;

  const rawCategory = String(row.category || "")
    .trim()
    .toLowerCase();
  const alias = INTEGRATION_CATEGORY_ALIASES[rawCategory];
  if (alias) return alias;

  return "other";
}

function groupIntegrationsByDisplayCategory(
  rows: McpServerRow[],
): Array<[IntegrationDisplayCategoryKey, McpServerRow[]]> {
  const buckets = new Map<IntegrationDisplayCategoryKey, McpServerRow[]>();
  for (const row of rows) {
    const key = integrationDisplayCategory(row);
    const group = buckets.get(key) || [];
    group.push(row);
    buckets.set(key, group);
  }
  return Array.from(buckets.entries()).sort(
    ([a], [b]) =>
      INTEGRATION_DISPLAY_CATEGORIES[a].rank -
      INTEGRATION_DISPLAY_CATEGORIES[b].rank,
  );
}

/** Row for an entity-level account (credential/api-key provider).
 *  Mirrors ConnectionRow's visual style but wires to the
 *  entity-accounts endpoints and invokes `onEdit` for per-row editing. */
function EntityAccountRow({
  account,
  serverKey,
  onEdit,
  onScanQr,
  showActions = true,
}: {
  account: {
    id: string;
    name: string | null;
    display_name: string | null;
    is_default: boolean;
    created_at: string | null;
    status: string;
    health: {
      ok: boolean | null;
      detail: string | null;
      checked_at: string | null;
      wiring?: {
        ok: boolean | null;
        detail: string | null;
        configured_url?: string | null;
        expected_url?: string | null;
        last_error?: string | null;
        pending_update_count?: number | null;
      } | null;
    } | null;
  };
  serverKey: string;
  onEdit: () => void;
  onScanQr?: (accountId: string) => void;
  showActions?: boolean;
}) {
  const queryClient = useQueryClient();
  const toast = useToastStore();

  const setDefault = useMutation({
    mutationFn: () =>
      api.integrations.setDefaultEntityAccount(serverKey, account.id),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["mcp-servers"] }),
  });

  const remove = useMutation({
    mutationFn: () =>
      api.integrations.deleteEntityAccount(serverKey, account.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["mcp-servers"] });
    },
  });

  const testNow = useMutation({
    mutationFn: () => api.integrations.testEntityAccount(account.id),
    onSuccess: (r) => {
      queryClient.invalidateQueries({ queryKey: ["mcp-servers"] });
      if (r.ok)
        toast.success(t("page.integrations.connection_ok"), r.detail || "");
      else
        toast.error(t("page.integrations.connection_failed"), r.detail || "");
    },
  });

  const registerWebhook = useMutation({
    mutationFn: () => api.integrations.registerWebhook(account.id),
    onSuccess: (r) => {
      queryClient.invalidateQueries({ queryKey: ["mcp-servers"] });
      if (r.registered)
        toast.success(t("page.integrations.webhook_registered"), r.url || "");
      else
        toast.error(
          t("page.integrations.could_not_register_webhook"),
          r.detail || r.reason || "",
        );
    },
  });

  const label =
    (account.name || account.display_name || "").trim() ||
    providerAccountFallback(serverKey);
  const hasWiring = !!account.health?.wiring;
  const wiringBroken = hasWiring && account.health?.wiring?.ok === false;

  const items = [
    { key: "edit", label: t("action.edit") },
    ...(serverKey === "wechat_personal" && onScanQr
      ? [{ key: "scan", label: t("page.integrations.scan_qr_status") }]
      : []),
    {
      key: "test",
      label: testNow.isPending
        ? t("page.integrations.testing")
        : t("page.integrations.test_connection"),
    },
    ...(wiringBroken
      ? [
          {
            key: "register-webhook",
            label: registerWebhook.isPending
              ? t("page.integrations.registering")
              : t("page.integrations.register_webhook"),
          },
        ]
      : []),
    ...(!account.is_default
      ? [{ key: "default", label: t("page.integrations.set_as_default") }]
      : []),
    {
      key: "remove",
      label: t("page.task_detail.runtime.remove_rule"),
      danger: true,
    },
  ];

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "6px 10px",
        borderRadius: 8,
        background: account.is_default
          ? "rgba(168,162,158,0.12)"
          : "rgba(250,250,249,0.7)",
        border: `1px solid ${account.is_default ? "rgba(168,162,158,0.35)" : "rgba(231,229,228,0.6)"}`,
        fontSize: 12,
      }}
    >
      <span
        style={{
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: account.status === "active" ? "#54a176" : "#d6d3d1",
          flexShrink: 0,
        }}
      />
      <span
        style={{
          flex: 1,
          minWidth: 0,
          color: "#44403c",
          fontWeight: 500,
          whiteSpace: "nowrap" as const,
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        {label}
        {account.name &&
          account.display_name &&
          account.display_name !== account.name && (
            <span style={{ color: "#a8a29e", fontWeight: 400, marginLeft: 6 }}>
              · {account.display_name}
            </span>
          )}
      </span>
      <HealthPip health={account.health} busy={testNow.isPending} />
      {account.is_default && <DefaultBadge />}
      {showActions && (
        <MoreMenu
          items={items}
          onSelect={(key) => {
            if (key === "edit") onEdit();
            else if (key === "scan") onScanQr?.(account.id);
            else if (key === "test") testNow.mutate();
            else if (key === "register-webhook") registerWebhook.mutate();
            else if (key === "default") setDefault.mutate();
            else if (key === "remove") {
              if (
                confirm(
                  t("page.integrations.remove_account_confirm").replace(
                    "{name}",
                    label,
                  ),
                )
              )
                remove.mutate();
            }
          }}
        />
      )}
    </div>
  );
}

/** Small dot showing credential health. */
/** Combined status pip — merges credential health + inbound wiring into
 *  ONE indicator. Dot colour reflects the worst of the two: green when
 *  everything's fine, amber for degraded (creds OK but wiring off), red
 *  for outright credential failure. Tooltip has the full breakdown. */
type HealthShape = {
  ok: boolean | null;
  detail: string | null;
  checked_at: string | null;
  wiring?: {
    ok: boolean | null;
    detail: string | null;
    mode?: "webhook" | "polling" | null;
    configured_url?: string | null;
    expected_url?: string | null;
    last_error?: string | null;
    pending_update_count?: number | null;
  } | null;
} | null;

function StatusPip({ health, busy }: { health: HealthShape; busy?: boolean }) {
  const wiring = health?.wiring || null;
  const credOk = health?.ok;
  const wireOk = wiring?.ok;

  // Colour = worst of credentials + wiring.
  // - busy          → amber (pulsing)
  // - untested      → grey
  // - cred fail     → red
  // - wire fail     → amber (credentials work, delivery path broken)
  // - all good      → green
  const color = busy
    ? "#cf9b44"
    : !health || credOk === null || credOk === undefined
      ? "#d6d3d1"
      : credOk === false
        ? "#d65f59"
        : wiring && wireOk === false
          ? "#cf9b44"
          : "#54a176";

  // Short inline label shown only when there's something worth flagging.
  let label: string | null = null;
  if (busy) label = t("page.integrations.testing");
  else if (!health) label = t("page.integrations.untested");
  else if (credOk === false) label = t("page.integrations.auth_failed");
  else if (wiring && wireOk === false)
    label =
      wiring.mode === "polling"
        ? t("page.integrations.polling_off")
        : t("page.integrations.webhook_off");

  // Tooltip — one line per concern, only included if it has something to say.
  const lines: string[] = [];
  if (busy) {
    lines.push(t("page.integrations.running_connection_test"));
  } else if (!health) {
    lines.push(t("page.integrations.not_tested_yet_use_menu_test_connection"));
  } else {
    lines.push(
      credOk
        ? t("page.integrations.credentials_reach_provider")
        : t("page.integrations.credentials_failed"),
    );
    if (health.detail) lines.push("  " + health.detail);
    if (wiring) {
      const mode =
        wiring.mode === "polling"
          ? t("page.integrations.polling_mode")
          : t("page.integrations.webhook_mode");
      lines.push(
        (wireOk
          ? t("page.integrations.inbound_registered")
          : t("page.integrations.inbound_not_working")
        ).replace("{mode}", mode),
      );
      if (wiring.detail) lines.push("  " + wiring.detail);
      if (wiring.configured_url)
        lines.push(
          `  ${t("page.integrations.registered_label")} ` +
            wiring.configured_url,
        );
      if (wiring.expected_url && wiring.expected_url !== wiring.configured_url)
        lines.push(
          `  ${t("page.integrations.expected_label")}   ` + wiring.expected_url,
        );
      if (wiring.last_error)
        lines.push(
          `  ${t("page.integrations.last_error_label")} ` + wiring.last_error,
        );
      if (wiring.pending_update_count)
        lines.push(
          t("page.integrations.pending_updates").replace(
            "{count}",
            String(wiring.pending_update_count),
          ),
        );
    }
    if (health.checked_at)
      lines.push(
        `  ${t("page.integrations.last_check_label")} ` +
          new Date(health.checked_at).toLocaleString(),
      );
  }

  return (
    <span
      title={lines.join("\n")}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        flexShrink: 0,
        padding: label ? "1px 8px 1px 5px" : 0,
        borderRadius: 999,
        background: label ? "#f5f5f4" : "transparent",
      }}
    >
      <span
        style={{
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: color,
          boxShadow: busy ? `0 0 6px ${color}80` : undefined,
          flexShrink: 0,
        }}
      />
      {label && (
        <span
          style={{
            fontSize: 10,
            fontWeight: 700,
            color: "#57534e",
            textTransform: "uppercase" as const,
            letterSpacing: "0.05em",
            whiteSpace: "nowrap" as const,
          }}
        >
          {label}
        </span>
      )}
    </span>
  );
}

// Backwards-compat aliases — HealthPip now takes health+wiring via the
// merged StatusPip. WiringPip is a no-op since StatusPip absorbs both.
const HealthPip = StatusPip;
const WiringPip = (_props: { wiring?: unknown }) => null;

function ConnectionRow({
  connection,
  serverKey,
  showActions,
  onReconnect,
}: {
  connection: {
    id: string;
    display_name: string | null;
    provider_user_id: string;
    expires_at: string | null;
    is_default: boolean;
    connected_at: string | null;
    health?: {
      ok: boolean | null;
      detail: string | null;
      checked_at: string | null;
      wiring?: {
        ok: boolean | null;
        detail: string | null;
        configured_url?: string | null;
        expected_url?: string | null;
        last_error?: string | null;
        pending_update_count?: number | null;
      } | null;
    } | null;
  };
  serverKey: string;
  showActions: boolean;
  onReconnect?: () => void;
}) {
  const queryClient = useQueryClient();
  const toast = useToastStore();

  const setDefault = useMutation({
    mutationFn: () =>
      api.integrations.setDefaultConnection(serverKey, connection.id),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["mcp-servers"] }),
  });

  const disconnect = useMutation({
    mutationFn: () =>
      api.integrations.disconnectAccount(serverKey, connection.id),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["mcp-servers"] }),
  });

  const testNow = useMutation({
    mutationFn: () => api.integrations.testOAuthConnection(connection.id),
    onSuccess: (r) => {
      queryClient.invalidateQueries({ queryKey: ["mcp-servers"] });
      if (r.ok)
        toast.success(t("page.integrations.connection_ok"), r.detail || "");
      else
        toast.error(t("page.integrations.connection_failed"), r.detail || "");
    },
  });

  const expired =
    connection.expires_at && new Date(connection.expires_at) < new Date();
  const label = connectionLabel(connection, t("status.connected"));
  const authFailed = connection.health?.ok === false;
  const needsReconnect = Boolean(onReconnect && (authFailed || expired));

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "6px 10px",
        borderRadius: 8,
        background: connection.is_default
          ? "rgba(168,162,158,0.12)"
          : "rgba(250,250,249,0.7)",
        border: `1px solid ${connection.is_default ? "rgba(168,162,158,0.35)" : "rgba(231,229,228,0.6)"}`,
        fontSize: 12,
      }}
    >
      <span
        style={{
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: expired ? "#a8a29e" : "#54a176",
          flexShrink: 0,
        }}
      />
      <span
        style={{
          flex: 1,
          minWidth: 0,
          color: "#44403c",
          fontWeight: 500,
          whiteSpace: "nowrap" as const,
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        {label}
      </span>
      {connection.is_default && <DefaultBadge />}
      {expired && <ExpiredBadge />}
      <HealthPip health={connection.health || null} busy={testNow.isPending} />
      <WiringPip wiring={connection.health?.wiring || null} />
      {showActions && needsReconnect && (
        <Button
          variant="outline"
          size="sm"
          onClick={() => onReconnect?.()}
          style={{ flexShrink: 0 }}
        >
          {t("page.integrations.reconnect")}
        </Button>
      )}
      {showActions && (
        <MoreMenu
          items={[
            ...(onReconnect
              ? [
                  {
                    key: "reconnect",
                    label: t("page.integrations.reconnect"),
                  },
                ]
              : []),
            {
              key: "test",
              label: testNow.isPending
                ? t("page.integrations.testing")
                : t("page.integrations.test_connection"),
            },
            ...(!connection.is_default
              ? [
                  {
                    key: "default",
                    label: t("page.integrations.set_as_default"),
                  },
                ]
              : []),
            {
              key: "disconnect",
              label: t("page.integrations.disconnect"),
              danger: true,
            },
          ]}
          onSelect={(key) => {
            if (key === "reconnect") onReconnect?.();
            else if (key === "test") testNow.mutate();
            else if (key === "default") setDefault.mutate();
            else if (key === "disconnect") {
              if (
                confirm(
                  t("page.integrations.disconnect_account_confirm").replace(
                    "{name}",
                    label,
                  ),
                )
              ) {
                disconnect.mutate();
              }
            }
          }}
        />
      )}
    </div>
  );
}
