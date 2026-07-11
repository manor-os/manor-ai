import { useState, useRef, useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { api, ApiError } from "../lib/api";
import type { BookingLink, CalendarConnectionOption, CalendarSettings, CalendarWorkingHourWindow, PeopleContext, PeopleContextActionResponse } from "../lib/types";
import { useToastStore } from "../stores/toast";
import StatusBadge from "../components/ui/StatusBadge";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import Modal from "../components/ui/Modal";
import PageHeader from "../components/ui/PageHeader";
import Button from "../components/ui/Button";
import Select from "../components/ui/Select";
import WorkingHoursEditor from "../components/ui/WorkingHoursEditor";
import { IconBuilding, IconCalendar, IconCopy, IconExternalLink, IconLink, IconPalette, IconPlus, IconTrash } from "../components/icons";
import NangoConnectButton from "../components/integrations/NangoConnectButton";
import { setPreferredTimeZone } from "../lib/format";
import { useAuthStore } from "../stores/auth";
import {
  THEME_STORAGE_KEY,
  applyThemePreference,
  getStoredThemePreference,
  type ThemePreference,
} from "../lib/theme";

import { t } from "../lib/i18n";

function leaveSessionOrLogout() {
  localStorage.removeItem("manor_token");
  window.location.href = "/login";
}

function invalidateEntityScopedQueries(queryClient: ReturnType<typeof useQueryClient>) {
  const queryKeys = [
    ["entity-me"],
    ["staff"],
    ["notifications"],
    ["billing-me"],
    ["billing-balance"],
    ["billing-plan"],
    ["billing-payments"],
    ["billing-credits"],
    ["usage-daily"],
    ["usage-by-source"],
    ["workspaces"],
    ["workspaces-trash"],
    ["my-models"],
    ["llm-config"],
    ["model-catalog"],
  ];
  queryKeys.forEach((queryKey) => {
    queryClient.invalidateQueries({ queryKey });
  });
}

async function applyPeopleContextResult(
  result: PeopleContextActionResponse,
  queryClient: ReturnType<typeof useQueryClient>,
) {
  if (result.access_token) {
    localStorage.setItem("manor_token", result.access_token);
    useAuthStore.setState({ token: result.access_token });
  }
  queryClient.setQueryData(["people-me"], result.context);
  const nextUser = await api.auth.me();
  localStorage.setItem("manor_user", JSON.stringify(nextUser));
  setPreferredTimeZone(nextUser.timezone);
  useAuthStore.setState({ user: nextUser });
  queryClient.setQueryData(["auth-me"], nextUser);
  invalidateEntityScopedQueries(queryClient);
  return nextUser;
}

type ThemeOption = {
  value: ThemePreference;
  labelKey: string;
  descriptionKey: string;
};

const APPEARANCE_OPTIONS: ThemeOption[] = [
  {
    value: "white",
    labelKey: "settings.theme_white",
    descriptionKey: "page.account.theme_white_description",
  },
  {
    value: "dark",
    labelKey: "settings.theme_dark",
    descriptionKey: "page.account.theme_dark_description",
  },
  {
    value: "auto",
    labelKey: "settings.theme_auto",
    descriptionKey: "page.account.theme_auto_description",
  },
];

export function AppearanceSection({ embedded = false }: { embedded?: boolean }) {
  const [themePreference, setThemePreference] = useState<ThemePreference>(
    getStoredThemePreference,
  );

  useEffect(() => {
    applyThemePreference(themePreference);
    window.localStorage.setItem(THEME_STORAGE_KEY, themePreference);
    if (themePreference !== "auto" || !window.matchMedia) return undefined;
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const handleSystemThemeChange = () => applyThemePreference("auto");
    media.addEventListener?.("change", handleSystemThemeChange);
    return () => media.removeEventListener?.("change", handleSystemThemeChange);
  }, [themePreference]);

  return (
    <section
      className={`account-appearance-section${embedded ? "" : " glass-panel"}`}
      style={{
        padding: embedded ? 0 : "28px 30px",
        borderRadius: embedded ? 0 : 28,
      }}
    >
      <div className="account-appearance-header">
        <div className="account-appearance-icon" aria-hidden="true">
          <IconPalette size={18} />
        </div>
        <div>
          <h2>{t("page.account.appearance")}</h2>
          <p>{t("page.account.appearance_description")}</p>
        </div>
      </div>

      <div className="account-theme-grid" role="radiogroup" aria-label={t("settings.theme")}>
        {APPEARANCE_OPTIONS.map((option) => {
          const active = themePreference === option.value;
          return (
            <button
              key={option.value}
              type="button"
              className={`account-theme-card${active ? " is-active" : ""}`}
              role="radio"
              aria-checked={active}
              onClick={() => setThemePreference(option.value)}
            >
              <span className="account-theme-preview" data-theme-preview={option.value}>
                <span />
                <span />
                <span />
              </span>
              <span className="account-theme-copy">
                <strong>{t(option.labelKey)}</strong>
                <span>{t(option.descriptionKey)}</span>
              </span>
              <span className="account-theme-check" aria-hidden="true">
                {active ? "✓" : ""}
              </span>
            </button>
          );
        })}
      </div>
    </section>
  );
}
/* ─── AI Model role metadata ─── */
type ModelRole = {
  key: string;
  label: string;
  desc: string;
  icon: string;
  color: string;
  /** When true, options render but the picker is read-only. */
  locked?: boolean;
  lockedReason?: string;
};
const MODEL_ROLES: ModelRole[] = [
  {
    key: "primary",
    label: t("page.account.primary_ai"),
    desc: t("page.account.chat_reasoning_complex_tasks_tool_calling"),
    icon: "M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z",
    color: "#6d6fb2",
  },
  {
    key: "worker",
    label: t("page.account.worker_ai"),
    desc: t("page.account.summaries_classification_automations_simple_jobs"),
    icon: "M3.75 13.5l10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75z",
    color: "#4f7d75",
  },
  {
    key: "embedding",
    label: t("page.account.embedding"),
    desc: t("page.account.text_embeddings_for_knowledge_base_search_rag"),
    icon: "M4.5 6.75A2.25 2.25 0 016.75 4.5h10.5a2.25 2.25 0 012.25 2.25v10.5a2.25 2.25 0 01-2.25 2.25H6.75a2.25 2.25 0 01-2.25-2.25V6.75zm4.5 2.25h6m-6 3h6m-6 3h3",
    color: "#9079c2",
  },
  {
    key: "image",
    label: t("page.account.image"),
    desc: t("page.account.image_generation_and_editing"),
    icon: "M2.25 15.75l5.159-5.159a2.25 2.25 0 013.182 0l5.159 5.159m-1.5-1.5l1.409-1.409a2.25 2.25 0 013.182 0l2.909 2.909M3.75 21h16.5A2.25 2.25 0 0022.5 18.75V5.25A2.25 2.25 0 0020.25 3H3.75A2.25 2.25 0 001.5 5.25v13.5A2.25 2.25 0 003.75 21z",
    color: "#c96a98",
  },
  {
    key: "video",
    label: t("page.account.video"),
    desc: t("page.account.video_generation_text_to_video"),
    icon: "M15.75 10.5l4.72-4.72a.75.75 0 011.28.53v11.38a.75.75 0 01-1.28.53l-4.72-4.72M4.5 18.75h9a2.25 2.25 0 002.25-2.25v-9a2.25 2.25 0 00-2.25-2.25h-9A2.25 2.25 0 002.25 7.5v9a2.25 2.25 0 002.25 2.25z",
    color: "#e11d48",
  },
  {
    key: "voice",
    label: t("page.account.text_to_speech"),
    desc: t("page.account.openrouter_tts_voice_generation"),
    icon: "M12 18.75a6 6 0 006-6v-1.5m-6 7.5a6 6 0 01-6-6v-1.5m6 7.5v3.75m-3.75 0h7.5M12 15.75a3 3 0 003-3V5.25a3 3 0 00-6 0v7.5a3 3 0 003 3z",
    color: "#6f4ba8",
  },
  {
    key: "audio",
    label: t("page.account.music_score"),
    desc: t("page.account.music_score_generation"),
    icon: "M9 9l10.5-3m0 6.553v3.75a2.25 2.25 0 11-1.5-2.122V7.5L9 10.071v7.232a2.25 2.25 0 11-1.5-2.122V9.75",
    color: "#cf9b44",
  },
  {
    key: "sfx",
    label: t("page.account.sound_effects"),
    desc: t("page.account.ambience_sfx_transition_generation"),
    icon: "M12 3v18m7.5-13.5v9m-15-9v9m11.25-12v15m-7.5-15v15",
    color: "#5a8ea6",
  },
  {
    key: "stt",
    label: t("page.account.speech_to_text"),
    desc: t("page.account.voice_transcription_via_microphone"),
    icon: "M19.114 5.636a9 9 0 010 12.728M16.463 8.288a5.25 5.25 0 010 7.424M6.75 8.25l4.72-4.72a.75.75 0 011.28.53v15.88a.75.75 0 01-1.28.53l-4.72-4.72H4.51c-.88 0-1.704-.507-1.938-1.354A9.01 9.01 0 012.25 12c0-.83.112-1.633.322-2.396C2.806 8.756 3.63 8.25 4.51 8.25H6.75z",
    color: "#4f9c84",
  },
];

/* ─── Left: ID Card ─── */
function IDCard({ user }: { user: any }) {
  const queryClient = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const initials = (user.display_name || user.email || "U")
    .split(" ")
    .map((w: string) => w[0])
    .join("")
    .toUpperCase()
    .slice(0, 2);

  const handleAvatar = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      await api.auth.uploadAvatar(file);
      queryClient.invalidateQueries({ queryKey: ["auth-me"] });
    } catch {
      /* */
    }
  };

  const joined = user.created_at
    ? new Date(user.created_at).toLocaleDateString("en-US", {
        year: "numeric",
        month: "long",
        day: "numeric",
      })
    : "Recently";

  return (
    <div
      className="account-id-card glass-card"
      style={{
        width: 280,
        flexShrink: 0,
        padding: "36px 28px",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        borderRadius: 28,
      }}
    >
      {/* Avatar */}
      <div
        onClick={() => fileRef.current?.click()}
        className="group"
        style={{
          width: 96,
          height: 96,
          borderRadius: "50%",
          cursor: "pointer",
          position: "relative",
          background: user.avatar_url
            ? "#f5f5f4"
            : "linear-gradient(135deg, #436b65, #5f928a)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          overflow: "hidden",
          boxShadow: "0 6px 20px rgba(67,107,101,0.2)",
          marginBottom: 20,
          transition: "transform 0.2s, box-shadow 0.2s",
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.transform = "scale(1.06)";
          e.currentTarget.style.boxShadow = "0 8px 28px rgba(67,107,101,0.3)";
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.transform = "scale(1)";
          e.currentTarget.style.boxShadow = "0 6px 20px rgba(67,107,101,0.2)";
        }}
      >
        {user.avatar_url ? (
          <img
            src={user.avatar_url}
            alt=""
            style={{ width: 96, height: 96, objectFit: "cover" }}
          />
        ) : (
          <span style={{ fontSize: 34, fontWeight: 800, color: "#fff" }}>
            {initials}
          </span>
        )}
        {/* Hover camera overlay */}
        <div
          style={{
            position: "absolute",
            inset: 0,
            background: "rgba(0,0,0,0.4)",
            borderRadius: "50%",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            opacity: 0,
            transition: "opacity 0.2s",
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.opacity = "1";
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.opacity = "0";
          }}
        >
          <svg
            width="22"
            height="22"
            fill="none"
            viewBox="0 0 24 24"
            stroke="#fff"
            strokeWidth={2}
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M6.827 6.175A2.31 2.31 0 015.186 7.23c-.38.054-.757.112-1.134.175C2.999 7.58 2.25 8.507 2.25 9.574V18a2.25 2.25 0 002.25 2.25h15A2.25 2.25 0 0021.75 18V9.574c0-1.067-.75-1.994-1.802-2.169a47.865 47.865 0 00-1.134-.175 2.31 2.31 0 01-1.64-1.055l-.822-1.316a2.192 2.192 0 00-1.736-1.039 48.774 48.774 0 00-5.232 0 2.192 2.192 0 00-1.736 1.039l-.821 1.316z"
            />
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M16.5 12.75a4.5 4.5 0 11-9 0 4.5 4.5 0 019 0z"
            />
          </svg>
        </div>
        <input
          ref={fileRef}
          type="file"
          accept="image/*"
          hidden
          onChange={handleAvatar}
        />
      </div>

      {/* Name */}
      <h2
        style={{
          fontSize: 20,
          fontWeight: 800,
          color: "#1c1917",
          margin: "0 0 4px",
          textAlign: "center",
          lineHeight: 1.3,
        }}
      >
        {user.display_name || user.email}
      </h2>
      <p
        style={{
          fontSize: 13,
          color: "#a8a29e",
          margin: "0 0 14px",
          textAlign: "center",
          wordBreak: "break-all",
        }}
      >
        {user.email}
      </p>

      {/* Role */}
      <div style={{ marginBottom: 24 }}>
        <StatusBadge
          type={
            user.role === "owner" || user.role === "admin" ? "purple" : "info"
          }
        >
          {user.role === "owner" ? t("page.users.role_admin") : user.role}
        </StatusBadge>
      </div>

      {/* Joined */}
      <div
        className="glass-card-sm"
        style={{
          width: "100%",
          padding: "14px 18px",
          marginBottom: 24,
          background: "rgba(242,246,245,0.6)",
          border: "1px solid rgba(79,125,117,0.1)",
        }}
      >
        <div
          style={{
            fontSize: 11,
            fontWeight: 600,
            color: "#78716c",
            textTransform: "uppercase",
            letterSpacing: "0.05em",
          }}
        >
          {t("page.account.joined")}
        </div>
        <div
          style={{
            fontSize: 14,
            fontWeight: 700,
            color: "#1c1917",
            marginTop: 4,
          }}
        >
          {joined}
        </div>
      </div>

      {/* Logout */}
      <button
        className="btn-manor-danger"
        onClick={leaveSessionOrLogout}
        style={{ width: "100%" }}
      >
        {t("action.logout")}
      </button>
    </div>
  );
}

/* ─── Company Card ─── */
function CompanyCard({
  user,
  context,
  focusInviteToken,
}: {
  user: any;
  context?: PeopleContext | null;
  focusInviteToken?: string;
}) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [entityName, setEntityName] = useState("");
  const [entityAddress, setEntityAddress] = useState("");
  const [entityPhone, setEntityPhone] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [switchingEntityId, setSwitchingEntityId] = useState<string | null>(null);
  const [companyActionError, setCompanyActionError] = useState("");
  const [inviteActionId, setInviteActionId] = useState<string | null>(null);
  const [dismissedInvitePromptToken, setDismissedInvitePromptToken] = useState<string | null>(null);

  const entity = context?.active_entity || null;
  const activeMembership = context?.active_membership || null;
  const billing = context?.billing || null;
  const canEditCompany =
    context?.effective_permissions?.includes("entity.update")
    || user.role === "owner"
    || user.role === "admin";
  const memberships = (context?.memberships ?? []).filter(
    (membership) => membership.status === "active",
  );
  const pendingInvites = [...(context?.pending_invites || [])].sort((a, b) => {
    if (focusInviteToken && a.invite_token === focusInviteToken) return -1;
    if (focusInviteToken && b.invite_token === focusInviteToken) return 1;
    return (a.entity_name || "").localeCompare(b.entity_name || "");
  });
  const declinedInvites = context?.declined_invites || [];
  const focusedInvite = focusInviteToken
    ? pendingInvites.find((invite) => invite.invite_token === focusInviteToken) || null
    : null;
  const showInvitePrompt = Boolean(
    focusedInvite && dismissedInvitePromptToken !== focusInviteToken,
  );
  const closeInvitePrompt = () => {
    if (focusInviteToken) {
      setDismissedInvitePromptToken(focusInviteToken);
    }
  };

  const openEdit = () => {
    if (!entity) return;
    setEntityName(entity.name || "");
    setEntityAddress(entity.address || "");
    setEntityPhone(entity.phone || "");
    setEditing(true);
    setError("");
  };

  const handleSave = async () => {
    setSaving(true);
    setError("");
    try {
      await api.entities.update({
        name: entityName,
        address: entityAddress || undefined,
        phone: entityPhone || undefined,
      });
      queryClient.invalidateQueries({ queryKey: ["entity-me"] });
      queryClient.invalidateQueries({ queryKey: ["people-me"] });
      setEditing(false);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to save");
    }
    setSaving(false);
  };

  const handleSwitch = async (entityId: string) => {
    setSwitchingEntityId(entityId);
    setCompanyActionError("");
    try {
      const result = await api.people.switchMembership(entityId);
      await applyPeopleContextResult(result, queryClient);
      setSwitchingEntityId(null);
    } catch (err) {
      setCompanyActionError(err instanceof ApiError ? err.message : "Failed to switch company");
      setSwitchingEntityId(null);
    }
  };

  const handleInviteAction = async (
    inviteId: string,
    action: "accept" | "decline",
  ): Promise<boolean> => {
    setInviteActionId(inviteId);
    setCompanyActionError("");
    try {
      const result = action === "accept"
        ? await api.people.acceptInvite(inviteId)
        : await api.people.declineInvite(inviteId);
      await applyPeopleContextResult(result, queryClient);
      return true;
    } catch (err) {
      setCompanyActionError(
        err instanceof ApiError
          ? err.message
          : action === "accept"
            ? "Failed to accept invite"
            : "Failed to decline invite",
      );
      return false;
    } finally {
      setInviteActionId(null);
    }
  };

  const handlePromptInviteAction = async (action: "accept" | "decline") => {
    if (!focusedInvite) return;
    const succeeded = await handleInviteAction(focusedInvite.invite_id, action);
    if (succeeded) {
      closeInvitePrompt();
    }
  };

  return (
    <>
      <div
        className="glass-card-sm"
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "16px 20px",
          background: "rgba(242,246,245,0.5)",
          border: "1px solid rgba(79,125,117,0.12)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
          <div
            style={{
              width: 44,
              height: 44,
              borderRadius: 12,
              background: "linear-gradient(135deg, #436b6522, #5f928a44)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              flexShrink: 0,
            }}
          >
            <IconBuilding size={20} style={{ color: "#436b65" }} />
          </div>
          <div>
            <p
              style={{
                fontSize: 14,
                fontWeight: 700,
                color: "#1c1917",
                margin: "0 0 2px",
              }}
            >
              {entity?.name || activeMembership?.entity_name || "—"}
            </p>
            <p style={{ fontSize: 12, color: "#a8a29e", margin: 0 }}>
              {entity?.address || t("page.account.no_address_set")}
            </p>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 7 }}>
              {activeMembership && (
                <span style={{
                  fontSize: 11,
                  fontWeight: 700,
                  color: "#436b65",
                  background: "rgba(79,125,117,0.08)",
                  border: "1px solid rgba(79,125,117,0.14)",
                  borderRadius: 999,
                  padding: "3px 8px",
                  textTransform: "capitalize",
                }}>
                  {activeMembership.staff_role_name || activeMembership.role || "member"}
                </span>
              )}
            </div>
          </div>
        </div>
        {canEditCompany && (
          <button
            className="btn-manor-teal-light"
            onClick={openEdit}
            style={{ padding: "6px 14px", fontSize: 12 }}
          >
            {t("page.account.change")}
          </button>
        )}
      </div>

      {pendingInvites.length > 0 && (
        <div style={{
          display: "flex",
          flexDirection: "column",
          gap: 8,
          marginTop: 10,
          maxWidth: 560,
        }}>
          <p style={{ margin: "0 0 2px", fontSize: 11, fontWeight: 800, color: "#78716c", textTransform: "uppercase" }}>
            Pending invitations
          </p>
          {pendingInvites.map((invite) => {
            const focused = Boolean(focusInviteToken && invite.invite_token === focusInviteToken);
            const busy = inviteActionId === invite.invite_id;
            return (
              <div
                key={invite.invite_id}
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 12,
                  padding: "10px 12px",
                  borderRadius: 10,
                  border: focused
                    ? "1px solid rgba(79,125,117,0.34)"
                    : "1px solid rgba(231,229,228,0.95)",
                  background: focused ? "rgba(242,246,245,0.82)" : "rgba(250,250,249,0.72)",
                }}
              >
                <div style={{ minWidth: 0 }}>
                  <p style={{ margin: 0, fontSize: 13, fontWeight: 800, color: "#292524" }}>
                    {invite.entity_name || invite.entity_id}
                  </p>
                  <p style={{ margin: "2px 0 0", fontSize: 11, color: "#a8a29e" }}>
                    {invite.role_name || "Team member"} · {invite.email}
                  </p>
                </div>
                <div style={{ display: "flex", gap: 7, flexShrink: 0 }}>
                  <button
                    type="button"
                    className="btn-manor-teal-light"
                    disabled={busy || !invite.can_decline}
                    onClick={() => handleInviteAction(invite.invite_id, "decline")}
                    style={{ padding: "6px 10px", fontSize: 12, opacity: busy ? 0.55 : 1 }}
                  >
                    Decline
                  </button>
                  <button
                    type="button"
                    className="btn-manor"
                    disabled={busy || !invite.can_accept}
                    onClick={() => handleInviteAction(invite.invite_id, "accept")}
                    style={{ padding: "6px 12px", fontSize: 12, opacity: busy ? 0.55 : 1 }}
                  >
                    {busy ? t("page.login.please_wait") : "Accept"}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {memberships.length > 1 && (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 8,
            marginTop: 10,
            maxWidth: 560,
          }}
        >
          {memberships.map((membership: any) => {
            const current = membership.is_current || membership.entity_id === user.entity_id;
            return (
              <div
                key={membership.entity_id}
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 12,
                  padding: "10px 12px",
                  borderRadius: 10,
                  border: current
                    ? "1px solid rgba(79,125,117,0.18)"
                    : "1px solid rgba(231,229,228,0.95)",
                  background: current ? "rgba(242,246,245,0.62)" : "rgba(250,250,249,0.7)",
                }}
              >
                <div style={{ minWidth: 0 }}>
                  <p style={{ margin: 0, fontSize: 13, fontWeight: 700, color: "#292524" }}>
                    {membership.entity_name || membership.entity_id}
                  </p>
                  <p style={{ margin: "2px 0 0", fontSize: 11, color: "#a8a29e", textTransform: "capitalize" }}>
                    {membership.staff_role_name || membership.role || "member"}
                  </p>
                </div>
                <button
                  type="button"
                  className={current ? "btn-manor-teal-light" : "btn-manor-secondary"}
                  disabled={current || !membership.can_switch || switchingEntityId === membership.entity_id}
                  onClick={() => handleSwitch(membership.entity_id)}
                  style={{
                    padding: "6px 12px",
                    fontSize: 12,
                    opacity: current || !membership.can_switch || switchingEntityId === membership.entity_id ? 0.62 : 1,
                  }}
                >
                  {current
                    ? "Current"
                    : switchingEntityId === membership.entity_id
                      ? t("page.login.please_wait")
                      : "Switch"}
                </button>
              </div>
            );
          })}
        </div>
      )}

      {companyActionError && (
        <p style={{ fontSize: 12, color: "#c14a44", margin: "8px 0 0", maxWidth: 560 }}>
          {companyActionError}
        </p>
      )}

      {declinedInvites.length > 0 && (
        <div style={{ marginTop: 10, maxWidth: 560 }}>
          {declinedInvites.map((invite) => (
            <div
              key={invite.invite_id}
              style={{
                display: "flex",
                justifyContent: "space-between",
                gap: 12,
                padding: "9px 12px",
                borderRadius: 10,
                border: "1px solid rgba(236,200,197,0.8)",
                background: "rgba(248,240,239,0.54)",
                color: "#883a35",
                fontSize: 12,
                fontWeight: 700,
              }}
            >
              <span>{invite.entity_name || invite.entity_id}</span>
              <span>Declined</span>
            </div>
          ))}
        </div>
      )}

      <Modal
        open={showInvitePrompt}
        onClose={closeInvitePrompt}
        title="Join team invitation"
        maxWidth="440px"
        footer={
          <>
            <button
              type="button"
              className="btn-manor-outline"
              disabled={!focusedInvite?.can_decline || inviteActionId === focusedInvite?.invite_id}
              onClick={() => handlePromptInviteAction("decline")}
              style={{ opacity: inviteActionId === focusedInvite?.invite_id ? 0.55 : 1 }}
            >
              Decline
            </button>
            <button
              type="button"
              className="btn-manor"
              disabled={!focusedInvite?.can_accept || inviteActionId === focusedInvite?.invite_id}
              onClick={() => handlePromptInviteAction("accept")}
              style={{ opacity: inviteActionId === focusedInvite?.invite_id ? 0.55 : 1 }}
            >
              {inviteActionId === focusedInvite?.invite_id
                ? t("page.login.please_wait")
                : "Accept invite"}
            </button>
          </>
        }
      >
        {focusedInvite && (
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
              <div
                style={{
                  width: 44,
                  height: 44,
                  borderRadius: 12,
                  background: "linear-gradient(135deg, #436b6522, #5f928a44)",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  flexShrink: 0,
                }}
              >
                <IconBuilding size={20} style={{ color: "#436b65" }} />
              </div>
              <div style={{ minWidth: 0 }}>
                <p style={{ margin: 0, fontSize: 15, fontWeight: 800, color: "#1c1917" }}>
                  {focusedInvite.entity_name || "Company team"}
                </p>
                <p style={{ margin: "3px 0 0", fontSize: 12, color: "#a8a29e" }}>
                  {focusedInvite.role_name || "Team member"} · {focusedInvite.email}
                </p>
              </div>
            </div>
            <p style={{ margin: 0, fontSize: 13, lineHeight: 1.6, color: "#57534e" }}>
              You were invited to join this company workspace. Accepting will switch you into the company context and use the company team permissions.
            </p>
          </div>
        )}
      </Modal>

      <Modal
        open={editing}
        onClose={() => setEditing(false)}
        title={t("page.account.edit_company_details")}
        maxWidth="480px"
        footer={
          <>
            <button
              className="btn-manor-outline"
              onClick={() => setEditing(false)}
            >
              {t("action.cancel")}
            </button>
            <button
              className="btn-manor"
              onClick={handleSave}
              disabled={saving || !entityName.trim()}
            >
              {saving
                ? t("page.task_collections.saving")
                : t("page.account.save_changes")}
            </button>
          </>
        }
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <div>
            <label className="manor-label">
              {t("page.team_people.company_name")}
            </label>
            <input
              className="manor-input"
              value={entityName}
              onChange={(e) => setEntityName(e.target.value)}
              placeholder={t("page.account.your_company_name")}
              maxLength={200}
            />
          </div>
          <div>
            <label className="manor-label">
              {t("page.account.company_address")}
            </label>
            <input
              className="manor-input"
              value={entityAddress}
              onChange={(e) => setEntityAddress(e.target.value)}
              placeholder={t("page.account.123_main_st_city_state")}
              maxLength={500}
            />
          </div>
          <div>
            <label className="manor-label">{t("page.team_people.phone")}</label>
            <input
              className="manor-input"
              value={entityPhone}
              onChange={(e) => setEntityPhone(e.target.value)}
              placeholder="+1 (555) 000-0000"
            />
          </div>
          {error && (
            <p style={{ fontSize: 13, color: "#c14a44", margin: 0 }}>{error}</p>
          )}
        </div>
      </Modal>
    </>
  );
}

/* ─── AI Model Configuration ─── */
type CustomModelDraft = {
  model: string;
  apiKey: string;
  baseUrl: string;
  useSavedApiKey: boolean;
};

type CustomModelTestState = {
  status: "idle" | "testing" | "passed" | "failed";
  message?: string;
  testedSignature?: string;
  latencyMs?: number | null;
  testToken?: string | null;
};

export function AIModelSection({
  user,
  settingsSurface = false,
}: {
  user: any;
  settingsSurface?: boolean;
}) {
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const [saving, setSaving] = useState<string | null>(null);
  const [savingCustomRole, setSavingCustomRole] = useState<string | null>(null);
  const [showApiKeyRole, setShowApiKeyRole] = useState<string | null>(null);
  const [apiKeyErrors, setApiKeyErrors] = useState<Record<string, string>>({});
  const [baseUrlInputs, setBaseUrlInputs] = useState<Record<string, string>>(
    {},
  );
  const [expandedOwnRole, setExpandedOwnRole] = useState<string | null>(null);
  const [switchModelTabs, setSwitchModelTabs] = useState<
    Record<string, "openrouter" | "custom">
  >({});
  const [drafts, setDrafts] = useState<Record<string, CustomModelDraft>>({});
  const [testStates, setTestStates] = useState<
    Record<string, CustomModelTestState>
  >({});

  const { data: catalog } = useQuery({
    queryKey: ["model-catalog"],
    queryFn: () =>
      api.auth.getModelCatalog?.() ||
      Promise.resolve({ catalog: {}, defaults: {} }),
  });
  const { data: myModels } = useQuery({
    queryKey: ["my-models"],
    queryFn: () =>
      api.auth.getMyModels?.() ||
      Promise.resolve({ models: {}, user_models: {} }),
  });
  const { data: llmConfig } = useQuery({
    queryKey: ["llm-config"],
    queryFn: () => api.auth.getLlmConfig(),
  });

  useEffect(() => {
    if (!llmConfig) return;
    const roleBaseUrls = { ...((llmConfig as any).role_base_urls || {}) };
    if ((llmConfig as any).llm_base_url && !roleBaseUrls.primary) {
      roleBaseUrls.primary = (llmConfig as any).llm_base_url;
    }
    setBaseUrlInputs(roleBaseUrls);
  }, [llmConfig]);

  const handleSelectModel = async (role: string, modelId: string) => {
    setSaving(role);
    try {
      await (api.auth.updateMyModels?.({ models: { [role]: modelId } }) ||
        api.auth.updateProfile({ llm_model: modelId }));
      queryClient.setQueryData(["my-models"], (prev: any) => {
        if (!prev) return prev;
        return {
          ...prev,
          models: { ...(prev.models || {}), [role]: modelId },
          user_models: { ...(prev.user_models || {}), [role]: modelId },
        };
      });
      queryClient.invalidateQueries({ queryKey: ["my-models"] });
      queryClient.invalidateQueries({ queryKey: ["auth-me"] });
    } catch {
      /* */
    }
    setSaving(null);
  };

  const draftSignature = (role: string, draft: CustomModelDraft) =>
    [
      role,
      draft.model.trim(),
      draft.apiKey.trim(),
      draft.useSavedApiKey ? "saved" : "new",
      draft.baseUrl.trim().replace(/\/+$/, ""),
    ].join("|");

  const updateDraft = (role: string, patch: Partial<CustomModelDraft>) => {
    setDrafts((prev) => {
      const defaults: CustomModelDraft = {
        model: "",
        apiKey: "",
        baseUrl: baseUrlInputs[role] || "",
        useSavedApiKey: false,
      };
      const nextDraft = { ...defaults, ...(prev[role] || {}), ...patch };
      setTestStates((states) => {
        const current = states[role];
        if (!current || current.status !== "passed") return states;
        const nextSignature = draftSignature(role, nextDraft);
        if (current.testedSignature === nextSignature) return states;
        return {
          ...states,
          [role]: {
            status: "idle",
            message: t("page.account.retest_required_after_changes"),
          },
        };
      });
      return { ...prev, [role]: nextDraft };
    });
  };

  const handleTestCustomModel = async (role: string) => {
    const draft = drafts[role];
    if (!draft) return;
    const signature = draftSignature(role, draft);
    setTestStates((prev) => ({ ...prev, [role]: { status: "testing" } }));
    setApiKeyErrors((prev) => ({ ...prev, [role]: "" }));
    try {
      const result = await api.auth.testMyModel({
        role,
        model: draft.model.trim(),
        api_key: draft.apiKey.trim() || undefined,
        use_saved_api_key: draft.useSavedApiKey && !draft.apiKey.trim(),
        base_url: draft.baseUrl.trim() || undefined,
      });
      setTestStates((prev) => ({
        ...prev,
        [role]: {
          status: "passed",
          message: result.detail || t("page.account.model_test_passed"),
          testedSignature: signature,
          latencyMs: result.latency_ms,
          testToken: result.test_token,
        },
      }));
      toast.success(t("page.account.model_test_passed"));
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.message
          : t("page.account.model_test_failed");
      setTestStates((prev) => ({
        ...prev,
        [role]: { status: "failed", message },
      }));
      toast.error(t("page.account.model_test_failed"), message);
    }
  };

  const handleSaveCustomModel = async (role: string) => {
    const draft = drafts[role];
    const testState = testStates[role];
    if (
      !draft ||
      testState?.status !== "passed" ||
      testState.testedSignature !== draftSignature(role, draft)
    )
      return;
    setSavingCustomRole(role);
    setApiKeyErrors((prev) => ({ ...prev, [role]: "" }));
    try {
      await api.auth.saveCustomModel({
        role,
        model: draft.model.trim(),
        api_key: draft.apiKey.trim() || undefined,
        use_saved_api_key: draft.useSavedApiKey && !draft.apiKey.trim(),
        base_url: draft.baseUrl.trim() || undefined,
        test_token: testState.testToken,
      });
      setDrafts((prev) => ({
        ...prev,
        [role]: { ...draft, apiKey: "", useSavedApiKey: true },
      }));
      setExpandedOwnRole(null);
      queryClient.invalidateQueries({ queryKey: ["llm-config"] });
      queryClient.invalidateQueries({ queryKey: ["my-models"] });
      queryClient.invalidateQueries({ queryKey: ["auth-me"] });
      toast.success(t("page.account.custom_model_settings_saved"));
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.message
          : t("page.account.failed_to_save_custom_model_settings");
      setApiKeyErrors((prev) => ({ ...prev, [role]: message }));
      toast.error(t("page.account.save_failed"), message);
    } finally {
      setSavingCustomRole(null);
    }
  };

  const handleSaveCatalogByok = async (role: string) => {
    const draft = drafts[role];
    if (!draft) return;
    setSavingCustomRole(role);
    setApiKeyErrors((prev) => ({ ...prev, [role]: "" }));
    try {
      if (draft.apiKey.trim()) {
        await api.auth.saveLlmApiKey(draft.apiKey.trim(), role);
      }
      await api.auth.saveLlmBaseUrl(draft.baseUrl.trim(), role);
      await (api.auth.updateMyModels?.({
        models: { [role]: draft.model.trim() },
      }) || api.auth.updateProfile({ llm_model: draft.model.trim() }));
      queryClient.setQueryData(["my-models"], (prev: any) => {
        if (!prev) return prev;
        return {
          ...prev,
          models: {
            ...(prev.models || {}),
            [role]: draft.model.trim(),
          },
          user_models: {
            ...(prev.user_models || {}),
            [role]: draft.model.trim(),
          },
        };
      });
      setDrafts((prev) => ({
        ...prev,
        [role]: { ...draft, apiKey: "", useSavedApiKey: true },
      }));
      setExpandedOwnRole(null);
      queryClient.invalidateQueries({ queryKey: ["llm-config"] });
      queryClient.invalidateQueries({ queryKey: ["my-models"] });
      queryClient.invalidateQueries({ queryKey: ["auth-me"] });
      toast.success(t("page.account.byok_settings_saved"));
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.message
          : t("page.account.failed_to_save_byok_settings");
      setApiKeyErrors((prev) => ({ ...prev, [role]: message }));
      toast.error(t("page.account.save_failed"), message);
    } finally {
      setSavingCustomRole(null);
    }
  };

  const handleClearApiKey = async (role: string) => {
    try {
      await api.auth.saveLlmApiKey("", role);

      const roleOptions: any[] = (catalog as any)?.catalog?.[role] || [];
      const currentRoleModel =
        (myModels as any)?.models?.[role] ||
        (catalog as any)?.defaults?.[role] ||
        "";
      const isRoleCustomModel =
        currentRoleModel &&
        !roleOptions.find((o: any) => o.id === currentRoleModel);
      let resetModel = "";
      // Cloud can reset to the hosted catalog route. OSS has no hosted
      // model route, so clearing a key must leave the selected model intact.

      setApiKeyErrors((prev) => ({ ...prev, [role]: "" }));
      setBaseUrlInputs((prev) => ({ ...prev, [role]: "" }));
      setDrafts((prev) => ({
        ...prev,
        [role]: {
          ...(prev[role] || {
            model: "",
            apiKey: "",
            baseUrl: "",
            useSavedApiKey: false,
          }),
          model: resetModel || prev[role]?.model || currentRoleModel || "",
          apiKey: "",
          baseUrl: "",
          useSavedApiKey: false,
        },
      }));
      setTestStates((prev) => ({ ...prev, [role]: { status: "idle" } }));
      queryClient.invalidateQueries({ queryKey: ["llm-config"] });
      queryClient.invalidateQueries({ queryKey: ["my-models"] });
      queryClient.invalidateQueries({ queryKey: ["auth-me"] });
    } catch (err) {
      setApiKeyErrors((prev) => ({
        ...prev,
        [role]:
          err instanceof ApiError ? err.message : "Failed to reset API key",
      }));
    }
  };

  const catalogData = (catalog as any)?.catalog || {};
  const defaults = (catalog as any)?.defaults || {};
  const resolvedModels = (myModels as any)?.models || {};

  const tierColors: Record<string, string> = {
    free: "#4f9c84",
    cheap: "#4f7d75",
    budget: "#5f84bd",
    balanced: "#9079c2",
    premium: "#b27c34",
  };
  const qualityIcons: Record<string, string> = {
    basic: "1",
    good: "2",
    high: "3",
    highest: "4",
  };
  const modelPrice: Record<
    string,
    { in?: number; out?: number; unit?: "token" | "second" }
  > = {
    "anthropic/claude-sonnet-4.6": { in: 3.0, out: 15.0, unit: "token" },
    "anthropic/claude-opus-4.7": { in: 5.0, out: 25.0, unit: "token" },
    "anthropic/claude-opus-4.6": { in: 5.0, out: 25.0, unit: "token" },
    "anthropic/claude-haiku-4.5": { in: 1.0, out: 5.0, unit: "token" },
    "openai/gpt-5.5": { in: 5.0, out: 30.0, unit: "token" },
    "openai/gpt-5.5-pro": { in: 30.0, out: 180.0, unit: "token" },
    "moonshotai/kimi-k2.6": { in: 0.74, out: 3.49, unit: "token" },
    "qwen/qwen3.6-plus": { in: 0.325, out: 1.95, unit: "token" },
    "deepseek/deepseek-v4-pro": { in: 0.435, out: 0.87, unit: "token" },
    "deepseek/deepseek-v4-flash": { in: 0.14, out: 0.28, unit: "token" },
    "openai/gpt-4.1": { in: 2.0, out: 8.0, unit: "token" },
    "openai/gpt-4.1-mini": { in: 0.4, out: 1.6, unit: "token" },
    "openai/gpt-4o": { in: 2.5, out: 10.0, unit: "token" },
    "openai/gpt-4o-mini": { in: 0.15, out: 0.6, unit: "token" },
    "google/gemini-2.5-pro": { in: 1.25, out: 10.0, unit: "token" },
    "google/gemini-2.5-flash": { in: 0.3, out: 2.5, unit: "token" },
    "google/gemini-2.5-flash-lite": { in: 0.1, out: 0.4, unit: "token" },
    "openai/gpt-5-image-mini": { in: 2.5, out: 2.0, unit: "token" },
    "google/gemini-3.1-flash-image": { in: 0.5, out: 3.0, unit: "token" },
    "google/gemini-3.1-flash-image-preview": {
      in: 0.5,
      out: 3.0,
      unit: "token",
    },
    "openai/gpt-5.4-image-2": { in: 8.0, out: 15.0, unit: "token" },
    "google/gemini-3.1-flash-tts-preview": {
      in: 1.0,
      out: 20.0,
      unit: "token",
    },
    "zyphra/zonos-v0.1-hybrid": { in: 7.0, out: 0, unit: "token" },
    "zyphra/zonos-v0.1-transformer": { in: 7.0, out: 0, unit: "token" },
    "sesame/csm-1b": { in: 7.0, out: 0, unit: "token" },
    "google/lyria-3-clip-preview": { in: 0.04, unit: "second" },
    "google/lyria-3-pro-preview": { in: 0.08, unit: "second" },
    "openai/gpt-4o-audio-preview": { in: 2.5, out: 10.0, unit: "token" },
    "openai/gpt-audio-mini": { in: 0.6, out: 2.4, unit: "token" },
    "openai/gpt-audio": { in: 2.5, out: 10.0, unit: "token" },
    "bytedance/seedance-2.0": { in: 0.134, unit: "second" },
    "bytedance/seedance-2.0-fast": { in: 0.107, unit: "second" },
    "kwaivgi/kling-v3.0-std": { in: 0.126, unit: "second" },
    "kwaivgi/kling-v3.0-pro": { in: 0.168, unit: "second" },
    "mxbai-embed-large": { in: 0, out: 0, unit: "token" },
  };
  const priceTierDollar = (id: string, fallbackTier?: string) => {
    const p = modelPrice[id];
    if (p) {
      // Convert price to a normalized "cost class" and show $ .. $$$$
      // Text/image: use input+output per 1M tokens. Video: use $/second.
      const cost =
        p.unit === "second" ? (p.in || 0) * 20 : (p.in || 0) + (p.out || 0);
      if (cost < 1) return "$";
      if (cost < 4) return "$$";
      if (cost < 12) return "$$$";
      return "$$$$";
    }
    if (fallbackTier === "free") return "Free";
    if (fallbackTier === "cheap") return "$";
    if (fallbackTier === "budget") return "$$";
    if (fallbackTier === "balanced") return "$$$";
    return "$$$$";
  };

  const apiKeyHint = (roleKey: string) => {
      if (roleKey === "image") {
        return "Self-hosted image generation needs a matching OpenAI or Google API key from your provider account.";
      }
      if (roleKey === "video") {
        return "Self-hosted video generation needs a matching Seedance or Kling API key from your provider account.";
      }
      if (roleKey === "stt") {
        return "Self-hosted speech-to-text needs a matching OpenAI speech API key from your provider account.";
      }
      return "Self-hosted model calls use the API key from your provider account for the selected model.";
  };

  let visibleModelRoles = MODEL_ROLES.filter((role) =>
    ["primary", "worker", "embedding", "image", "video", "stt"].includes(role.key),
  );

  const PROVIDER_BASE_URLS: Record<string, string> = {
    openai: "https://api.openai.com/v1",
    anthropic: "https://api.anthropic.com/v1",
    deepseek: "https://api.deepseek.com/v1",
    google: "https://generativelanguage.googleapis.com/v1beta/openai",
    mistral: "https://api.mistral.ai/v1",
    groq: "https://api.groq.com/openai/v1",
    cohere: "https://api.cohere.ai/v2",
    together: "https://api.together.xyz/v1",
    perplexity: "https://api.perplexity.ai",
    fireworks: "https://api.fireworks.ai/inference/v1",
    xai: "https://api.x.ai/v1",
    moonshot: "https://api.moonshot.cn/v1",
    moonshotai: "https://api.moonshot.cn/v1",
    qwen: "https://dashscope.aliyuncs.com/compatible-mode/v1",
  };

  const inferBaseUrl = (modelId: string): string => {
    const prefix = modelId.split("/")[0].toLowerCase();
    return PROVIDER_BASE_URLS[prefix] || "";
  };

  return (
    <div
      className={`account-ai-section${settingsSurface ? " account-ai-section--settings" : ""}`}
      style={{
        marginTop: settingsSurface ? 0 : 32,
        paddingTop: settingsSurface ? 0 : 28,
        borderTop: settingsSurface ? "none" : "1px solid rgba(28,25,23,0.06)",
      }}
    >
      <h3 className="manor-section-title">{t("page.account.ai_models")}</h3>
      <p className="manor-section-subtitle">
        {t("page.account.configure_which_ai_models_to_use_for_different_t")}
      </p>

      <div className="account-ai-role-list" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {visibleModelRoles.map((role) => {
          const options = catalogData[role.key] || [];
          const currentModel =
            resolvedModels[role.key] || defaults[role.key] || "";
          const currentOption = options.find((o: any) => o.id === currentModel);
          const isCustomModel = currentModel && !currentOption;
          const isOwnExpanded = expandedOwnRole === role.key;
          const roleApiKeys = (llmConfig as any)?.role_api_keys || {};
          const savedApiKey =
            roleApiKeys[role.key] ||
            (role.key === "primary" ? (llmConfig as any)?.llm_api_key : "");
          const hasRoleApiKey = !!savedApiKey;
          const draft = drafts[role.key] || {
            model: currentModel,
            apiKey: "",
            baseUrl: baseUrlInputs[role.key] || "",
            useSavedApiKey: hasRoleApiKey,
          };
          const testState = testStates[role.key] || { status: "idle" as const };
          const currentSignature = draftSignature(role.key, draft);
          const canUseCustomModel = ["primary", "worker", "embedding"].includes(
            role.key,
          );
          const canUseCatalogByok = ["image", "video", "stt"].includes(
            role.key,
          );
          const canUseOwnProvider = canUseCustomModel || canUseCatalogByok;
          const canSaveCustom =
            canUseCustomModel &&
            !!draft.model.trim() &&
            (!!draft.apiKey.trim() || draft.useSavedApiKey) &&
            testState.status === "passed" &&
            testState.testedSignature === currentSignature &&
            savingCustomRole !== role.key;
          const canSaveCatalogByok =
            canUseCatalogByok &&
            !!draft.model.trim() &&
            (!!draft.apiKey.trim() || draft.useSavedApiKey || hasRoleApiKey) &&
            savingCustomRole !== role.key;
          const canEditModelId = canUseCustomModel || canUseCatalogByok;
          const canEditProviderSettings = canUseCustomModel || canUseCatalogByok;
          const apiKeyError = apiKeyErrors[role.key] || "";
          const showApiKey = showApiKeyRole === role.key;
          let defaultPanelTab: "openrouter" | "custom" = "custom";
          const activePanelTab = switchModelTabs[role.key] || defaultPanelTab;
          let modelSourceBadgeBackground = "#f3ecd6";
          let modelSourceBadgeColor = "#936027";
          let modelSourceBadgeLabel = t("page.account.byok_required");
          let switchModelLabel = t("page.account.configure_byok");
          let catalogTabLabel = t("page.account.model_catalog");
          let catalogTabHint = t("page.account.catalog_tab_hint_oss");
          let clearApiKeyLabel = t("page.account.clear_byok_key");

          return (
            <div
              key={role.key}
              className="account-ai-role-card"
              style={{
                borderRadius: 16,
                overflow: "hidden",
                border: isOwnExpanded
                  ? "1.5px solid rgba(109,111,178,0.3)"
                  : "1px solid rgba(231,229,228,0.5)",
                background: "rgba(255,255,255,0.6)",
              }}
            >
              {/* ── Role header row ── */}
              <div
                className="account-ai-role-header"
                style={{
                  padding: "14px 18px",
                  display: "flex",
                  alignItems: "center",
                  gap: 14,
                }}
              >
                {/* Icon */}
                <div
                  style={{
                    width: 36,
                    height: 36,
                    borderRadius: 10,
                    flexShrink: 0,
                    background: `${role.color}08`,
                    border: `1.5px solid ${role.color}15`,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                  }}
                >
                  <svg
                    width="16"
                    height="16"
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke={role.color}
                    strokeWidth={1.5}
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  >
                    <path d={role.icon} />
                  </svg>
                </div>

                {/* Label */}
                <div
                  className="account-ai-role-label"
                  style={{ flex: "0 0 130px", minWidth: 0 }}
                >
                  <p
                    style={{
                      fontSize: 13,
                      fontWeight: 600,
                      color: "#1c1917",
                      margin: 0,
                    }}
                  >
                    {role.label}
                  </p>
                  <p
                    style={{
                      fontSize: 10,
                      color: "#a8a29e",
                      margin: "2px 0 0",
                      lineHeight: 1.3,
                    }}
                  >
                    {role.desc}
                  </p>
                </div>

                {/* Active model display */}
                <div
                  className="account-ai-model-options"
                  style={{ flex: 1, minWidth: 0 }}
                >
                  {role.locked && role.lockedReason && (
                    <div
                      style={{
                        padding: "4px 8px",
                        borderRadius: 6,
                        background: "#fafaf9",
                        fontSize: 10,
                        color: "#78716c",
                        display: "flex",
                        alignItems: "center",
                        gap: 4,
                      }}
                    >
                      <svg
                        width={10}
                        height={10}
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth={2}
                      >
                        <path
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          d="M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z"
                        />
                      </svg>
                      <span>{role.lockedReason}</span>
                    </div>
                  )}
                  {currentModel && !isOwnExpanded && (
                    <div
                      style={{
                        display: "inline-flex",
                        alignItems: "center",
                        gap: 6,
                        padding: "5px 10px",
                        borderRadius: 8,
                        background: isCustomModel
                          ? "rgba(109,111,178,0.06)"
                          : `${role.color}06`,
                        border: isCustomModel
                          ? "1.5px solid rgba(109,111,178,0.25)"
                          : `1.5px solid ${role.color}30`,
                      }}
                    >
                      <span
                        style={{
                          fontSize: 12,
                          fontWeight: 600,
                          color: isCustomModel ? "#6d6fb2" : role.color,
                        }}
                      >
                        {currentOption ? currentOption.name : currentModel}
                      </span>
                      <span
                        style={{
                          fontSize: 9,
                          fontWeight: 700,
                          padding: "1px 5px",
                          borderRadius: 4,
                          background: hasRoleApiKey
                            ? "rgba(109,111,178,0.1)"
                            : modelSourceBadgeBackground,
                          color: hasRoleApiKey ? "#6d6fb2" : modelSourceBadgeColor,
                        }}
                      >
                        {hasRoleApiKey
                          ? t("page.account.your_own_key")
                          : modelSourceBadgeLabel}
                      </span>
                    </div>
                  )}
                </div>

                {/* Right side: switch model toggle */}
                <div
                  className="account-ai-role-actions"
                  style={{
                    flexShrink: 0,
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                  }}
                >
                  {/* Toggle button */}
                  {!role.locked && canUseOwnProvider && (
                    <button
                      onClick={() => {
                        const opening = !isOwnExpanded;
                        setExpandedOwnRole(opening ? role.key : null);
                        if (opening) {
                          setSwitchModelTabs((prev) => ({
                            ...prev,
                            [role.key]: defaultPanelTab,
                          }));
                          setDrafts((prev) => ({
                            ...prev,
                            [role.key]: {
                              model: currentModel,
                              apiKey: "",
                              baseUrl: baseUrlInputs[role.key] || "",
                              useSavedApiKey: hasRoleApiKey,
                            },
                          }));
                          setTestStates((prev) => ({
                            ...prev,
                            [role.key]: { status: "idle" },
                          }));
                        }
                      }}
                      style={{
                        padding: "6px 12px",
                        borderRadius: 8,
                        fontSize: 11,
                        fontWeight: 600,
                        cursor: "pointer",
                        transition: "all 0.15s",
                        whiteSpace: "nowrap",
                        border: isOwnExpanded
                          ? "1.5px solid rgba(109,111,178,0.4)"
                          : "1px solid rgba(109,111,178,0.25)",
                        background: isOwnExpanded
                          ? "rgba(241,243,249,0.8)"
                          : "rgba(241,243,249,0.4)",
                        color: "#6d6fb2",
                      }}
                    >
                      <div
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: 5,
                        }}
                      >
                        <svg
                          width="11"
                          height="11"
                          fill="none"
                          viewBox="0 0 24 24"
                          stroke="currentColor"
                          strokeWidth={2}
                        >
                          <path
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            d="M7.5 21L3 16.5m0 0L7.5 12M3 16.5h13.5m0-13.5L21 7.5m0 0L16.5 12M21 7.5H7.5"
                          />
                        </svg>
                        <span>{switchModelLabel}</span>
                        <svg
                          width="8"
                          height="8"
                          fill="none"
                          viewBox="0 0 24 24"
                          stroke="currentColor"
                          strokeWidth={3}
                        >
                          <path
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            d={
                              isOwnExpanded
                                ? "M4.5 15.75l7.5-7.5 7.5 7.5"
                                : "M19.5 8.25l-7.5 7.5-7.5-7.5"
                            }
                          />
                        </svg>
                      </div>
                    </button>
                  )}
                </div>
              </div>

              {/* ── Expanded: Switch Model panel ── */}
              {isOwnExpanded && !role.locked && (
                <div
                  style={{
                    borderTop: "1px solid rgba(28,25,23,0.06)",
                    background: "rgba(248,250,255,0.6)",
                  }}
                >
                  {/* Tab bar */}
                  <div
                    style={{
                      display: "flex",
                      borderBottom: "1px solid rgba(28,25,23,0.06)",
                      padding: "0 18px",
                    }}
                  >
                    {[
                      {
                        key: "openrouter" as const,
                        label: catalogTabLabel,
                        icon: "M12 21a9.004 9.004 0 008.716-6.747M12 21a9.004 9.004 0 01-8.716-6.747M12 21c2.485 0 4.5-4.03 4.5-9S14.485 3 12 3m0 18c-2.485 0-4.5-4.03-4.5-9S9.515 3 12 3m0 0a8.997 8.997 0 017.843 4.582M12 3a8.997 8.997 0 00-7.843 4.582m15.686 0A11.953 11.953 0 0112 10.5c-2.998 0-5.74-1.1-7.843-2.918m15.686 0A8.959 8.959 0 0121 12c0 .778-.099 1.533-.284 2.253m0 0A17.919 17.919 0 0112 16.5c-3.162 0-6.133-.815-8.716-2.247m0 0A9.015 9.015 0 013 12c0-1.605.42-3.113 1.157-4.418",
                      },
                      {
                        key: "custom" as const,
                        label: t("page.account.use_custom_model"),
                        icon: "M15.75 5.25a3 3 0 013 3m3 0a6 6 0 01-7.029 5.912c-.563-.097-1.159.026-1.563.43L10.5 17.25H8.25v2.25H6v2.25H2.25v-2.818c0-.597.237-1.17.659-1.591l6.499-6.499c.404-.404.527-1 .43-1.563A6 6 0 1121.75 8.25z",
                      },
                    ].map((tab) => {
                      const active = activePanelTab === tab.key;
                      return (
                        <button
                          key={tab.key}
                          type="button"
                          onClick={() =>
                            setSwitchModelTabs((prev) => ({
                              ...prev,
                              [role.key]: tab.key,
                            }))
                          }
                          style={{
                            padding: "10px 14px",
                            fontSize: 12,
                            fontWeight: active ? 600 : 500,
                            cursor: "pointer",
                            border: "none",
                            borderBottom: active
                              ? "2px solid #6d6fb2"
                              : "2px solid transparent",
                            background: "transparent",
                            color: active ? "#6d6fb2" : "#78716c",
                            display: "flex",
                            alignItems: "center",
                            gap: 5,
                            transition: "all 0.15s",
                          }}
                        >
                          <svg
                            width="12"
                            height="12"
                            fill="none"
                            viewBox="0 0 24 24"
                            stroke="currentColor"
                            strokeWidth={2}
                          >
                            <path
                              strokeLinecap="round"
                              strokeLinejoin="round"
                              d={tab.icon}
                            />
                          </svg>
                          {tab.label}
                        </button>
                      );
                    })}
                  </div>

                  {/* Tab content */}
                  <div style={{ padding: "14px 18px 16px" }}>
                    {/* ── OpenRouter tab ── */}
                    {activePanelTab === "openrouter" && (
                      <div
                        style={{
                          display: "flex",
                          flexDirection: "column",
                          gap: 10,
                        }}
                      >
                        <p
                          style={{
                            fontSize: 11,
                            color: "#78716c",
                            margin: 0,
                          }}
                        >
                          {catalogTabHint}
                        </p>
                        {options.length > 0 ? (
                          <div
                            style={{
                              display: "flex",
                              flexWrap: "wrap",
                              gap: 6,
                            }}
                          >
                            {options.map((opt: any) => {
                              const active =
                                currentModel === opt.id && !isCustomModel;
                              const isSaving = saving === role.key;
                              return (
                                <button
                                  key={opt.id}
                                  type="button"
                                  onClick={() => {
                                    handleSelectModel(role.key, opt.id);
                                    const autoUrl = inferBaseUrl(opt.id);
                                    updateDraft(role.key, {
                                      model: opt.id,
                                      ...(autoUrl && !draft.baseUrl
                                        ? { baseUrl: autoUrl }
                                        : {}),
                                    });
                                    setSwitchModelTabs((prev) => ({
                                      ...prev,
                                      [role.key]: "custom",
                                    }));
                                  }}
                                  disabled={isSaving}
                                  style={{
                                    padding: "7px 12px",
                                    borderRadius: 9,
                                    fontSize: 12,
                                    fontWeight: 500,
                                    cursor: isSaving ? "wait" : "pointer",
                                    transition: "all 0.15s",
                                    textAlign: "left" as const,
                                    border: active
                                      ? `2px solid ${role.color}`
                                      : "1px solid rgba(231,229,228,0.8)",
                                    background: active
                                      ? `${role.color}08`
                                      : "#fff",
                                    color: active ? role.color : "#57534e",
                                    opacity: isSaving && !active ? 0.6 : 1,
                                  }}
                                >
                                  <div
                                    style={{
                                      display: "flex",
                                      alignItems: "center",
                                      gap: 5,
                                    }}
                                  >
                                    <span style={{ fontWeight: 600 }}>
                                      {opt.name}
                                    </span>
                                    {opt.tag && (
                                      <span
                                        style={{
                                          fontSize: 8,
                                          fontWeight: 700,
                                          padding: "1px 4px",
                                          borderRadius: 4,
                                          background: active
                                            ? `${role.color}15`
                                            : "#f5f5f4",
                                          color: active
                                            ? role.color
                                            : "#a8a29e",
                                        }}
                                      >
                                        {opt.tag}
                                      </span>
                                    )}
                                  </div>
                                  <div
                                    style={{
                                      display: "flex",
                                      alignItems: "center",
                                      gap: 4,
                                      marginTop: 2,
                                    }}
                                  >
                                    {opt.tier && (
                                      <span
                                        style={{
                                          fontSize: 8,
                                          fontWeight: 600,
                                          color:
                                            tierColors[opt.tier] || "#a8a29e",
                                        }}
                                      >
                                        {priceTierDollar(opt.id, opt.tier)}
                                      </span>
                                    )}
                                    {opt.quality && (
                                      <span
                                        style={{ display: "flex", gap: 1 }}
                                      >
                                        {[1, 2, 3, 4].map((i) => (
                                          <span
                                            key={i}
                                            style={{
                                              width: 3,
                                              height: 7,
                                              borderRadius: 1,
                                              background:
                                                i <=
                                                Number(
                                                  qualityIcons[opt.quality] ||
                                                    0,
                                                )
                                                  ? active
                                                    ? role.color
                                                    : "#a8a29e"
                                                  : "#e7e5e4",
                                            }}
                                          />
                                        ))}
                                      </span>
                                    )}
                                  </div>
                                </button>
                              );
                            })}
                          </div>
                        ) : (
                          <p
                            style={{
                              fontSize: 11,
                              color: "#a8a29e",
                              margin: 0,
                            }}
                          >
                            {t(
                              "page.account.no_models_available_for_this_role_yet",
                            )}
                          </p>
                        )}
                      </div>
                    )}

                    {/* ── Custom model tab ── */}
                    {activePanelTab === "custom" && (
                      <div
                        style={{
                          display: "flex",
                          flexDirection: "column",
                          gap: 12,
                        }}
                      >
                        {canUseCatalogByok ? (
                          <p
                            style={{
                              fontSize: 11,
                              color: "#426c87",
                              margin: 0,
                              padding: 10,
                              borderRadius: 8,
                              background: "#e8eff4",
                            }}
                          >
                            {t("page.account.catalog_byok_saves_without_live_test")}
                          </p>
                        ) : !canUseCustomModel && (
                          <p
                            style={{
                              fontSize: 11,
                              color: "#936027",
                              margin: 0,
                              padding: 10,
                              borderRadius: 8,
                              background: "#f3ecd6",
                            }}
                          >
                            {t("page.account.custom_model_test_catalog_only")}
                          </p>
                        )}
                        {role.key === "embedding" && (
                          <p
                            style={{
                              fontSize: 11,
                              color: "#6f4ba8",
                              margin: 0,
                              padding: "8px 10px",
                              borderRadius: 8,
                              background: "rgba(144,121,194,0.07)",
                              border: "1px solid rgba(144,121,194,0.2)",
                            }}
                          >
                            {t(
                              "page.account.pinned_to_the_bundled_mxbai_embed_large_1024_dim_switc",
                            )}
                          </p>
                        )}

                        {/* Model ID */}
                        <div>
                          <label
                            className="manor-label"
                            style={{
                              marginBottom: 6,
                              display: "flex",
                              alignItems: "center",
                              gap: 6,
                            }}
                          >
                            <span
                              style={{
                                width: 18,
                                height: 18,
                                borderRadius: 6,
                                background: "#6d6fb2",
                                color: "#fff",
                                display: "inline-flex",
                                alignItems: "center",
                                justifyContent: "center",
                                fontSize: 10,
                                fontWeight: 800,
                              }}
                            >
                              1
                            </span>
                            {t("page.account.custom_model_id")}
                          </label>
                          <input
                            className="manor-input"
                            value={draft.model}
                            onChange={(e) => {
                              const newModel = e.target.value;
                              const autoUrl = inferBaseUrl(newModel);
                              updateDraft(role.key, {
                                model: newModel,
                                ...(autoUrl && !draft.baseUrl
                                  ? { baseUrl: autoUrl }
                                  : {}),
                              });
                            }}
                            placeholder="deepseek/deepseek-chat, openai/gpt-4.1, anthropic/claude-sonnet-4-6"
                            disabled={!canEditModelId}
                            style={{
                              width: "100%",
                              fontSize: 12,
                              padding: "7px 10px",
                              paddingRight: 32,
                              opacity: canEditModelId ? 1 : 0.75,
                            }}
                          />
                        </div>

                        {/* API Key + Base URL */}
                        <div>
                          <label
                            className="manor-label"
                            style={{
                              marginBottom: 4,
                              display: "flex",
                              alignItems: "center",
                              gap: 6,
                            }}
                          >
                            <span
                              style={{
                                width: 18,
                                height: 18,
                                borderRadius: 6,
                                background: "#6d6fb2",
                                color: "#fff",
                                display: "inline-flex",
                                alignItems: "center",
                                justifyContent: "center",
                                fontSize: 10,
                                fontWeight: 800,
                              }}
                            >
                              2
                            </span>
                            {role.label} {t("page.api_keys.api_key")}
                            {hasRoleApiKey && !draft.apiKey && (
                              <span
                                style={{
                                  fontSize: 9,
                                  fontWeight: 600,
                                  color: "#437f6b",
                                  background: "#e4efe8",
                                  padding: "1px 5px",
                                  borderRadius: 4,
                                }}
                              >
                                {t("page.blueprint_detail.saved")}
                              </span>
                            )}
                          </label>
                          <div
                            className="account-ai-key-grid"
                            style={{
                              display: "grid",
                              gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))",
                              gap: 10,
                            }}
                          >
                            <div style={{ position: "relative" }}>
                              <input
                                className="manor-input"
                                type={showApiKey ? "text" : "password"}
                                value={draft.apiKey}
                                onChange={(e) =>
                                  updateDraft(role.key, {
                                    apiKey: e.target.value,
                                    useSavedApiKey:
                                      hasRoleApiKey && !e.target.value.trim(),
                                  })
                                }
                                placeholder={
                                  hasRoleApiKey
                                    ? t(
                                        "page.account.native_key_saved_paste_new_to_replace",
                                      )
                                    : t(
                                        "page.account.native_provider_key_only_not_sk_or",
                                      )
                                }
                                disabled={!canEditProviderSettings}
                                style={{
                                  width: "100%",
                                  fontSize: 12,
                                  padding: "7px 10px",
                                  paddingRight: 32,
                                  opacity: canEditProviderSettings ? 1 : 0.55,
                                }}
                              />
                              <button
                                type="button"
                                onClick={() =>
                                  setShowApiKeyRole(
                                    showApiKey ? null : role.key,
                                  )
                                }
                                style={{
                                  position: "absolute",
                                  right: 6,
                                  top: 18,
                                  transform: "translateY(-50%)",
                                  background: "none",
                                  border: "none",
                                  cursor: "pointer",
                                  color: "#a8a29e",
                                  padding: 2,
                                }}
                              >
                                <svg
                                  width="12"
                                  height="12"
                                  fill="none"
                                  viewBox="0 0 24 24"
                                  stroke="currentColor"
                                  strokeWidth={2}
                                >
                                  {showApiKey ? (
                                    <>
                                      <path
                                        strokeLinecap="round"
                                        strokeLinejoin="round"
                                        d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"
                                      />
                                      <path
                                        strokeLinecap="round"
                                        strokeLinejoin="round"
                                        d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"
                                      />
                                    </>
                                  ) : (
                                    <path
                                      strokeLinecap="round"
                                      strokeLinejoin="round"
                                      d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M3 3l18 18"
                                    />
                                  )}
                                </svg>
                              </button>
                              {hasRoleApiKey && !draft.apiKey && (
                                <span
                                  style={{
                                    fontSize: 9,
                                    color: "#437f6b",
                                    marginTop: 2,
                                    display: "block",
                                    fontFamily: "monospace",
                                  }}
                                >
                                  {savedApiKey}
                                </span>
                              )}
                            </div>
                            <div>
                              <input
                                className="manor-input"
                                value={draft.baseUrl}
                                onChange={(e) =>
                                  updateDraft(role.key, {
                                    baseUrl: e.target.value,
                                  })
                                }
                                placeholder="https://api.example.com/v1"
                                disabled={!canEditProviderSettings}
                                style={{
                                  width: "100%",
                                  fontSize: 12,
                                  padding: "7px 10px",
                                  opacity: canEditProviderSettings ? 1 : 0.55,
                                }}
                              />
                              {draft.model && inferBaseUrl(draft.model) && (
                                <span
                                  style={{
                                    fontSize: 9,
                                    color: "#78716c",
                                    marginTop: 2,
                                    display: "block",
                                  }}
                                >
                                  {t("page.account.auto_detected_from_key")}:{" "}
                                  {inferBaseUrl(draft.model)}
                                </span>
                              )}
                            </div>
                          </div>
                          <p
                            style={{
                              fontSize: 10,
                              color: "#78716c",
                              margin: "4px 0 0",
                            }}
                          >
                            {apiKeyHint(role.key)}{" "}
                            {canUseCatalogByok
                              ? t("page.account.catalog_byok_saves_without_live_test")
                              : t("page.account.custom_model_changes_save_after_test")}
                          </p>
                          {hasRoleApiKey && !draft.apiKey && (
                            <button
                              type="button"
                              onClick={() => handleClearApiKey(role.key)}
                              style={{
                                marginTop: 6,
                                border: "1px solid rgba(209,139,134,0.35)",
                                background: "rgba(248,240,239,0.8)",
                                color: "#a23e38",
                                borderRadius: 7,
                                fontSize: 11,
                                fontWeight: 600,
                                cursor: "pointer",
                                padding: "6px 10px",
                              }}
                            >
                              {clearApiKeyLabel}
                            </button>
                          )}
                        </div>

                        {/* Test + Save buttons */}
                        <div
                          style={{
                            display: "flex",
                            flexWrap: "wrap",
                            alignItems: "center",
                            gap: 8,
                          }}
                        >
                          {canUseCustomModel && (
                            <button
                              type="button"
                              className="btn-manor-outline"
                              onClick={() => handleTestCustomModel(role.key)}
                              disabled={
                                testState.status === "testing" ||
                                !draft.model.trim() ||
                                (!draft.apiKey.trim() && !draft.useSavedApiKey)
                              }
                            >
                              {testState.status === "testing"
                                ? t("page.account.testing_model")
                                : t("page.account.test_model")}
                            </button>
                          )}
                          <button
                            type="button"
                            className="btn-manor"
                            onClick={() => {
                              if (canUseCatalogByok) {
                                handleSaveCatalogByok(role.key);
                              } else {
                                handleSaveCustomModel(role.key);
                              }
                            }}
                            disabled={canUseCatalogByok ? !canSaveCatalogByok : !canSaveCustom}
                            style={{ opacity: canUseCatalogByok ? (canSaveCatalogByok ? 1 : 0.5) : (canSaveCustom ? 1 : 0.5) }}
                          >
                            {savingCustomRole === role.key
                              ? t("page.task_collections.saving")
                              : t("page.account.save_model_settings")}
                          </button>
                          {testState.status === "passed" && (
                            <span
                              style={{
                                fontSize: 11,
                                color: "#437f6b",
                                fontWeight: 600,
                              }}
                            >
                              {testState.message ||
                                t("page.account.model_test_passed")}
                              {testState.latencyMs
                                ? ` (${testState.latencyMs} ms)`
                                : ""}
                            </span>
                          )}
                          {testState.status === "failed" && (
                            <span
                              style={{
                                fontSize: 11,
                                color: "#c14a44",
                                fontWeight: 600,
                              }}
                            >
                              {testState.message}
                            </span>
                          )}
                          {testState.status === "idle" && testState.message && (
                            <span
                              style={{
                                fontSize: 11,
                                color: "#b27c34",
                                fontWeight: 600,
                              }}
                            >
                              {testState.message}
                            </span>
                          )}
                        </div>
                        {apiKeyError && (
                          <p
                            style={{
                              fontSize: 10,
                              color: "#c14a44",
                              margin: 0,
                            }}
                          >
                            {apiKeyError}
                          </p>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

/* ─── Calendar & Booking ─── */
const CALENDAR_PROVIDER_OPTIONS = [
  { value: "", label: "Not connected" },
  { value: "google_calendar", label: "Google Calendar" },
  { value: "ms_calendar", label: "Outlook Calendar" },
];

const CALENDAR_PROVIDER_LABELS: Record<string, string> = {
  google_calendar: "Google Calendar",
  ms_calendar: "Outlook Calendar",
};

const BOOKING_LOCATION_OPTIONS = [
  { value: "video", label: "Video" },
  { value: "phone", label: "Phone" },
  { value: "in_person", label: "In person" },
  { value: "custom", label: "Custom" },
  { value: "none", label: "None" },
];

function defaultWorkingHours(): CalendarWorkingHourWindow[] {
  return Array.from({ length: 7 }, (_, day) => ({
    day_of_week: day,
    enabled: day < 5,
    start: "09:00",
    end: "17:00",
  }));
}

function looksLikeEmail(value?: string | null): boolean {
  return Boolean(value && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.trim()));
}

function calendarConnectionLabel(conn: CalendarConnectionOption): string {
  const display = (conn.display_name || "").trim();
  const providerUser = (conn.provider_user_id || "").trim();
  const label = looksLikeEmail(display)
    ? display
    : looksLikeEmail(providerUser)
      ? providerUser
      : display || providerUser || "Calendar account";
  return conn.is_default ? `${label} (default)` : label;
}

export function CalendarBookingSection({ user }: { user: any }) {
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const { data, isLoading } = useQuery({
    queryKey: ["calendar-settings"],
    queryFn: () => api.calendarSettings.get(),
  });

  const [provider, setProvider] = useState("");
  const [connectionId, setConnectionId] = useState("");
  const [defaultCalendarId, setDefaultCalendarId] = useState("primary");
  const [durationMinutes, setDurationMinutes] = useState(30);
  const [bufferAfterMinutes, setBufferAfterMinutes] = useState(10);
  const [minNoticeMinutes, setMinNoticeMinutes] = useState(120);
  const [rollingWindowDays, setRollingWindowDays] = useState(30);
  const [trackDeadlines, setTrackDeadlines] = useState(true);
  const [trackScheduled, setTrackScheduled] = useState(true);
  const [autoCreateEvents, setAutoCreateEvents] = useState(false);
  const [workingHours, setWorkingHours] = useState<CalendarWorkingHourWindow[]>(defaultWorkingHours);
  const [savingSettings, setSavingSettings] = useState(false);
  const [linkName, setLinkName] = useState("");
  const [linkDuration, setLinkDuration] = useState("30");
  const [linkLocationType, setLinkLocationType] = useState<BookingLink["location_type"]>("video");
  const [linkBusy, setLinkBusy] = useState(false);
  const [showLinkForm, setShowLinkForm] = useState(false);
  const [connectingProvider, setConnectingProvider] = useState<string | null>(null);

  useEffect(() => {
    const settings = data?.settings;
    if (!settings) return;
    const savedConnectionId = settings.connection_id || "";
    const matchedConnection = (data?.connections || []).find((conn) =>
      conn.id === savedConnectionId || conn.provider_user_id === savedConnectionId,
    );
    setProvider(settings.provider || "");
    setConnectionId(matchedConnection?.id || savedConnectionId);
    setDefaultCalendarId(settings.default_calendar_id || "primary");
    setDurationMinutes(settings.booking_defaults?.duration_minutes || 30);
    setBufferAfterMinutes(settings.booking_defaults?.buffer_after_minutes || 10);
    setMinNoticeMinutes(settings.booking_defaults?.min_notice_minutes || 120);
    setRollingWindowDays(settings.booking_defaults?.rolling_window_days || 30);
    setTrackDeadlines(settings.track_task_deadlines !== false);
    setTrackScheduled(settings.track_scheduled_tasks !== false);
    setAutoCreateEvents(Boolean(settings.auto_create_events_from_tasks));
    setWorkingHours(settings.working_hours?.length ? settings.working_hours : defaultWorkingHours());
    setLinkDuration(String(settings.booking_defaults?.duration_minutes || 30));
  }, [data?.settings, data?.connections, user?.timezone]);

  const providerConnections = (data?.connections || []).filter((conn) =>
    provider ? conn.provider === provider : true,
  );
  const bookingLinks = data?.settings?.booking_links || [];
  const selectedProviderLabel = CALENDAR_PROVIDER_LABELS[provider] || "Calendar";
  const parsedLinkDuration = Number.parseInt(linkDuration, 10);
  const linkDurationValid = Number.isFinite(parsedLinkDuration) && parsedLinkDuration >= 5 && parsedLinkDuration <= 480;

  const saveSettings = async () => {
    setSavingSettings(true);
    try {
      await api.calendarSettings.update({
        provider,
        connection_id: connectionId || null,
        default_calendar_id: defaultCalendarId || "primary",
        conflict_calendar_ids: [defaultCalendarId || "primary"],
        timezone: user?.timezone || data?.settings?.timezone || "UTC",
        working_hours: workingHours,
        booking_defaults: {
          duration_minutes: durationMinutes,
          buffer_before_minutes: 0,
          buffer_after_minutes: bufferAfterMinutes,
          min_notice_minutes: minNoticeMinutes,
          rolling_window_days: rollingWindowDays,
        },
        track_task_deadlines: trackDeadlines,
        track_scheduled_tasks: trackScheduled,
        auto_create_events_from_tasks: autoCreateEvents,
      } as Partial<CalendarSettings>);
      await queryClient.invalidateQueries({ queryKey: ["calendar-settings"] });
      await queryClient.invalidateQueries({ queryKey: ["calendar-agenda"] });
      toast.success("Calendar settings saved");
    } catch (err) {
      toast.error("Failed to save calendar settings", err instanceof Error ? err.message : undefined);
    } finally {
      setSavingSettings(false);
    }
  };

  const createLink = async () => {
    if (!linkName.trim() || !linkDurationValid) return;
    setLinkBusy(true);
    try {
      await api.calendarSettings.createBookingLink({
        name: linkName.trim(),
        duration_minutes: parsedLinkDuration,
        location_type: linkLocationType,
        calendar_id: defaultCalendarId || "primary",
      });
      setLinkName("");
      setShowLinkForm(false);
      await queryClient.invalidateQueries({ queryKey: ["calendar-settings"] });
      toast.success("Booking link created");
    } catch (err) {
      toast.error("Failed to create booking link", err instanceof Error ? err.message : undefined);
    } finally {
      setLinkBusy(false);
    }
  };

  const toggleLink = async (link: BookingLink) => {
    await api.calendarSettings.updateBookingLink(link.id, {
      name: link.name,
      enabled: !link.enabled,
    });
    await queryClient.invalidateQueries({ queryKey: ["calendar-settings"] });
  };

  const deleteLink = async (link: BookingLink) => {
    await api.calendarSettings.deleteBookingLink(link.id);
    await queryClient.invalidateQueries({ queryKey: ["calendar-settings"] });
    toast.success("Booking link deleted");
  };

  const bookingUrl = (link: BookingLink) => {
    if (link.url) {
      try {
        return `${window.location.origin}${new URL(link.url).pathname}`;
      } catch {
        if (link.url.startsWith("/")) return `${window.location.origin}${link.url}`;
      }
    }
    return `${window.location.origin}/book/u/${user.id}/${link.slug}`;
  };

  const copyLink = async (link: BookingLink) => {
    await navigator.clipboard?.writeText(bookingUrl(link));
    toast.success("Link copied");
  };

  const connectCalendarProvider = async () => {
    if (!provider) return;
    if (provider !== "google_calendar") return;
    setConnectingProvider(provider);
    try {
      const { authorize_url } = await api.integrations.oauthStart(provider, {
        returnTo: "/settings?tab=calendar",
      });
      window.location.href = authorize_url;
    } catch (err) {
      const message = err instanceof ApiError && err.status === 501
        ? "Google Calendar OAuth is not configured for this deployment."
        : err instanceof Error
          ? err.message
          : undefined;
      toast.error(`Failed to connect ${selectedProviderLabel}`, message);
      setConnectingProvider(null);
    }
  };

  return (
    <div
      className="calendar-booking-section"
      style={{
        marginTop: 0,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16, marginBottom: 12 }}>
        <div>
          <h3 className="manor-section-title" style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <IconCalendar size={18} /> Calendar & booking
          </h3>
          <p className="manor-section-subtitle">Calendar connection, availability, and booking links.</p>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <button
            className="btn-manor-ghost"
            type="button"
            onClick={() => setShowLinkForm((value) => !value)}
            style={{ display: "inline-flex", alignItems: "center", gap: 6, whiteSpace: "nowrap" }}
          >
            <IconPlus size={14} /> New booking link
          </button>
          <button className="btn-manor" disabled={savingSettings || isLoading} onClick={saveSettings}>
            {savingSettings ? "Saving..." : "Save"}
          </button>
        </div>
      </div>

      {isLoading ? (
        <div style={{ padding: "18px 0", display: "flex", alignItems: "center", gap: 10, color: "#78716c", fontSize: 13 }}>
          <LoadingSpinner size={16} /> Loading calendar settings
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 260px), 1fr))",
              gap: 14,
              maxWidth: 700,
            }}
          >
            <div>
              <label className="manor-label">Calendar app</label>
              <Select
                value={provider}
                onChange={(nextProvider) => {
                  setProvider(nextProvider);
                  setConnectionId("");
                }}
                options={CALENDAR_PROVIDER_OPTIONS}
              />
            </div>
            {provider && providerConnections.length > 0 && (
              <div>
                <label className="manor-label">Connected account</label>
                <Select
                  value={connectionId}
                  onChange={setConnectionId}
                  selectedOptionColor="#292524"
                  selectedOptionCheckColor="#5f928a"
                  options={[
                    { value: "", label: "Default account" },
                    ...providerConnections.map((conn) => ({
                      value: conn.id,
                      label: calendarConnectionLabel(conn),
                    })),
                  ]}
                />
              </div>
            )}
            {provider && providerConnections.length === 0 && (
              <div
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: 8,
                  justifyContent: "end",
                  minHeight: 68,
                }}
              >
                {provider === "google_calendar" ? (
                  <Button
                    variant="outline"
                    size="md"
                    loading={connectingProvider === provider}
                    disabled={Boolean(connectingProvider)}
                    onClick={connectCalendarProvider}
                    style={{ justifyContent: "center" }}
                  >
                    Connect {selectedProviderLabel}
                  </Button>
                ) : (
                  <NangoConnectButton
                    providerConfigKeys={[provider]}
                    label={`Connect ${selectedProviderLabel}`}
                    variant="outline"
                    size="md"
                    onConnected={() => {
                      queryClient.invalidateQueries({ queryKey: ["calendar-settings"] });
                    }}
                  />
                )}
                <span style={{ fontSize: 12, color: "#a8a29e", fontWeight: 650 }}>
                  Connect an account to check availability and create booking events.
                </span>
              </div>
            )}
          </div>

          {showLinkForm && (
            <div
              className="booking-link-form"
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 180px), 1fr))",
                gap: 10,
                maxWidth: 860,
                alignItems: "end",
                padding: "12px",
                borderRadius: 14,
                border: "1px solid rgba(79,125,117,0.18)",
                background: "rgba(79,125,117,0.055)",
              }}
            >
              <div>
                <label className="manor-label">Name</label>
                <input className="manor-input" value={linkName} onChange={(e) => setLinkName(e.target.value)} placeholder="Discovery call" />
              </div>
              <div>
                <label className="manor-label">Duration</label>
                <input
                  className="manor-input"
                  type="number"
                  min={5}
                  max={480}
                  value={linkDuration}
                  onChange={(e) => setLinkDuration(e.target.value)}
                />
              </div>
              <div>
                <label className="manor-label">Location</label>
                <Select value={linkLocationType} onChange={(v) => setLinkLocationType(v as BookingLink["location_type"])} options={BOOKING_LOCATION_OPTIONS} />
              </div>
              <button
                className="btn-manor"
                disabled={!linkName.trim() || !linkDurationValid || linkBusy}
                onClick={createLink}
                style={{ height: 40, display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 6, whiteSpace: "nowrap" }}
              >
                <IconPlus size={14} /> Create
              </button>
            </div>
          )}

          <WorkingHoursEditor
            value={workingHours.length ? workingHours : defaultWorkingHours()}
            onChange={(next) => setWorkingHours(next)}
          />

          <div>
            <div className="booking-links-heading" style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
              <IconLink size={14} style={{ color: "#78716c" }} />
              <span style={{ fontSize: 13, fontWeight: 800, color: "#292524" }}>Booking links</span>
            </div>

            <div className="booking-links-list" style={{ display: "flex", flexDirection: "column", gap: 8, maxWidth: 900, marginTop: 12 }}>
              {bookingLinks.length === 0 && (
                <div className="booking-links-empty" style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10, padding: "12px 14px", borderRadius: 12, background: "rgba(245,245,244,0.72)", color: "#78716c", fontSize: 12, fontWeight: 600 }}>
                  <span>No booking links yet.</span>
                  <button className="btn-manor-ghost" type="button" onClick={() => setShowLinkForm(true)} style={{ height: 30, padding: "0 10px", fontSize: 11 }}>
                    New
                  </button>
                </div>
              )}
              {bookingLinks.map((link) => (
                <div
                  key={link.id}
                  className="booking-link-row"
                  style={{
                    display: "flex",
                    flexWrap: "wrap",
                    alignItems: "center",
                    gap: 12,
                    padding: "12px 14px",
                    borderRadius: 14,
                    border: "1px solid rgba(231,229,228,0.82)",
                    background: "rgba(255,255,255,0.78)",
                  }}
                >
                  <div style={{ minWidth: 0, flex: "1 1 180px" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <span style={{ width: 8, height: 8, borderRadius: 99, background: link.enabled ? "#4f9c84" : "#a8a29e", flexShrink: 0 }} />
                      <span style={{ fontSize: 13, fontWeight: 800, color: "#292524", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{link.name}</span>
                    </div>
                    <span style={{ fontSize: 11, fontWeight: 600, color: "#a8a29e" }}>{link.duration_minutes} min · {link.location_type.replace("_", " ")}</span>
                  </div>
                  <div style={{ minWidth: 0, flex: "2 1 220px", display: "flex", alignItems: "center", gap: 8 }}>
                    <code className="booking-link-url" style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 11, color: "#57534e", background: "#f5f5f4", padding: "6px 8px", borderRadius: 8 }}>
                      {bookingUrl(link)}
                    </code>
                  </div>
                  <div style={{ display: "flex", alignItems: "center", gap: 6, flex: "0 0 auto" }}>
                    <button className="btn-manor-ghost" title="Copy" onClick={() => copyLink(link)} style={{ width: 30, height: 30, padding: 0, display: "inline-flex", alignItems: "center", justifyContent: "center" }}>
                      <IconCopy size={13} />
                    </button>
                    <a className="btn-manor-ghost" title="Open" href={bookingUrl(link)} target="_blank" rel="noreferrer" style={{ width: 30, height: 30, padding: 0, display: "inline-flex", alignItems: "center", justifyContent: "center" }}>
                      <IconExternalLink size={13} />
                    </a>
                    <button className="btn-manor-ghost" onClick={() => toggleLink(link)} style={{ height: 30, padding: "0 9px", fontSize: 11, fontWeight: 800 }}>
                      {link.enabled ? "On" : "Off"}
                    </button>
                    <button className="btn-manor-ghost" title="Delete" onClick={() => deleteLink(link)} style={{ width: 30, height: 30, padding: 0, display: "inline-flex", alignItems: "center", justifyContent: "center", color: "#c14a44" }}>
                      <IconTrash size={13} />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/* ─── AI File Permissions ─── */
type FilePermissionMode = "approval" | "always_approve" | "deny";

const FILE_PERMISSION_OPTIONS: Array<{
  value: FilePermissionMode;
  label: string;
  description: string;
  tone: string;
}> = [
  {
    value: "approval",
    label: t("page.account.ask_every_time"),
    description: t(
      "page.account.ai_must_ask_before_creating_editing_deleting_or_saving",
    ),
    tone: "#4f7d75",
  },
  {
    value: "always_approve",
    label: t("page.account.always_approve"),
    description: t(
      "page.account.ai_may_change_user_visible_files_without_a_prompt_inte",
    ),
    tone: "#b27c34",
  },
  {
    value: "deny",
    label: t("page.account.deny"),
    description: t(
      "page.account.ai_cannot_change_user_visible_files_reading_and_intern",
    ),
    tone: "#c14a44",
  },
];

export function FilePermissionSection() {
  const queryClient = useQueryClient();
  const [saving, setSaving] = useState<FilePermissionMode | null>(null);
  const { data: preferences } = useQuery({
    queryKey: ["preferences"],
    queryFn: () => api.admin.getPreferences(),
  });

  const rawMode = String((preferences as any)?.ai_file_permission || "approval")
    .toLowerCase()
    .replace("-", "_");
  const currentMode: FilePermissionMode =
    rawMode === "deny"
      ? "deny"
      : rawMode === "always_approve" ||
          rawMode === "always_approval" ||
          rawMode === "always approval"
        ? "always_approve"
        : "approval";

  const handleSelect = async (mode: FilePermissionMode) => {
    setSaving(mode);
    try {
      await api.admin.updatePreferences({ ai_file_permission: mode });
      queryClient.invalidateQueries({ queryKey: ["preferences"] });
    } finally {
      setSaving(null);
    }
  };

  return (
    <div
      style={{
        marginTop: 32,
        paddingTop: 28,
        borderTop: "1px solid rgba(28,25,23,0.06)",
      }}
    >
      <h3 className="manor-section-title">
        {t("page.account.ai_file_permissions")}
      </h3>
      <p className="manor-section-subtitle">
        {t("page.account.controls_ai_changes_to_user_visible_knowledge_fi")}
      </p>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 180px), 1fr))",
          gap: 10,
          maxWidth: 680,
        }}
      >
        {FILE_PERMISSION_OPTIONS.map((option) => {
          const active = currentMode === option.value;
          const isSaving = saving === option.value;
          return (
            <button
              key={option.value}
              type="button"
              onClick={() => handleSelect(option.value)}
              disabled={!!saving}
              style={{
                textAlign: "left",
                padding: "14px 16px",
                borderRadius: 14,
                border: active
                  ? `2px solid ${option.tone}`
                  : "1px solid rgba(231,229,228,0.7)",
                background: active
                  ? `${option.tone}08`
                  : "rgba(255,255,255,0.72)",
                color: "#1c1917",
                cursor: saving ? "wait" : "pointer",
                opacity: saving && !isSaving ? 0.55 : 1,
                transition: "all 0.15s ease",
              }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 8,
                }}
              >
                <span
                  style={{
                    fontSize: 13,
                    fontWeight: 800,
                    color: active ? option.tone : "#1c1917",
                  }}
                >
                  {option.label}
                </span>
                {active && (
                  <span
                    style={{
                      fontSize: 10,
                      fontWeight: 800,
                      color: option.tone,
                    }}
                  >
                    {isSaving
                      ? t("page.account.saving")
                      : t("page.workspaces.filter_active")}
                  </span>
                )}
              </div>
              <p
                style={{
                  margin: "7px 0 0",
                  fontSize: 11,
                  lineHeight: 1.45,
                  color: "#78716c",
                }}
              >
                {option.description}
              </p>
            </button>
          );
        })}
      </div>
    </div>
  );
}

/* ─── Security Section ─── */
export function SecuritySection() {
  const [currentPwd, setCurrentPwd] = useState("");
  const [newPwd, setNewPwd] = useState("");
  const [confirmPwd, setConfirmPwd] = useState("");
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setSuccess("");
    if (newPwd !== confirmPwd) {
      setError(t("page.account.new_passwords_do_not_match"));
      return;
    }
    if (newPwd.length < 8) {
      setError(t("page.account.password_must_be_at_least_8_characters"));
      return;
    }
    setLoading(true);
    try {
      await api.auth.changePassword(currentPwd, newPwd);
      setSuccess("Password changed successfully");
      setCurrentPwd("");
      setNewPwd("");
      setConfirmPwd("");
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Failed to change password",
      );
    }
    setLoading(false);
  };

  return (
    <div
      style={{
        marginTop: 32,
        paddingTop: 28,
        borderTop: "1px solid rgba(28,25,23,0.06)",
      }}
    >
      <h3 className="manor-section-title">{t("page.account.security")}</h3>
      <p className="manor-section-subtitle">
        {t("page.account.update_your_password_to_keep_your_account_secure")}
      </p>
      <form
        onSubmit={handleSubmit}
        style={{
          display: "flex",
          flexDirection: "column",
          gap: 14,
          maxWidth: 400,
        }}
      >
        <div>
          <label className="manor-label">
            {t("page.account.current_password")}
          </label>
          <input
            type="password"
            value={currentPwd}
            onChange={(e) => setCurrentPwd(e.target.value)}
            className="manor-input"
            required
          />
        </div>
        <div>
          <label className="manor-label">
            {t("page.reset_password.new_password")}
          </label>
          <input
            type="password"
            value={newPwd}
            onChange={(e) => setNewPwd(e.target.value)}
            className="manor-input"
            required
          />
        </div>
        <div>
          <label className="manor-label">
            {t("page.account.confirm_new_password")}
          </label>
          <input
            type="password"
            value={confirmPwd}
            onChange={(e) => setConfirmPwd(e.target.value)}
            className="manor-input"
            required
          />
        </div>
        {error && (
          <p style={{ fontSize: 13, color: "#c14a44", margin: 0 }}>{error}</p>
        )}
        {success && (
          <p style={{ fontSize: 13, color: "#437f6b", margin: 0 }}>{success}</p>
        )}
        <div>
          <button
            type="submit"
            disabled={loading}
            className="btn-manor"
            style={{ opacity: loading ? 0.5 : 1 }}
          >
            {loading
              ? t("page.task_collections.saving")
              : t("page.account.change_password")}
          </button>
        </div>
      </form>
    </div>
  );
}

/* ─── Main Account Page ─── */
export default function Account() {
  const queryClient = useQueryClient();
  const [searchParams] = useSearchParams();
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [phone, setPhone] = useState("");
  const [timezone, setTimezone] = useState("UTC");
  const [saving, setSaving] = useState(false);
  const [success, setSuccess] = useState("");
  const [formError, setFormError] = useState("");
  const [inited, setInited] = useState(false);

  const { data: user, isLoading } = useQuery({
    queryKey: ["auth-me"],
    queryFn: () => api.auth.me(),
  });
  const { data: peopleContext } = useQuery({
    queryKey: ["people-me"],
    queryFn: () => api.people.me(),
    enabled: Boolean(user),
  });
  const focusInviteToken =
    searchParams.get("team_invite")
    || searchParams.get("invite_token")
    || searchParams.get("invite")
    || "";

  if (user && !inited) {
    setFirstName(user.first_name || "");
    setLastName(user.last_name || "");
    setPhone(user.phone || "");
    setTimezone(user.timezone || "UTC");
    setInited(true);
  }

  const handleSave = async () => {
    setSaving(true);
    setFormError("");
    setSuccess("");
    try {
      const displayName =
        [firstName, lastName].filter(Boolean).join(" ") || undefined;
      const updatedUser = await api.auth.updateProfile({
        first_name: firstName || undefined,
        last_name: lastName || undefined,
        phone: phone || undefined,
        display_name: displayName,
        timezone,
      });
      setPreferredTimeZone(updatedUser.timezone);
      localStorage.setItem("manor_user", JSON.stringify(updatedUser));
      queryClient.invalidateQueries({ queryKey: ["auth-me"] });
      setSuccess("Profile saved");
      setTimeout(() => setSuccess(""), 3000);
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "Failed to save");
    }
    setSaving(false);
  };

  if (isLoading || !user) {
    return (
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          height: "100%",
          color: "#a8a29e",
        }}
      >
        <LoadingSpinner size={28} />
      </div>
    );
  }

  return (
    <div
      className="account-page"
      style={{
        height: "100%",
        overflowY: "auto",
        padding: "1.5rem 2rem",
        animation: "fade-in 0.3s ease-out",
      }}
    >
      {/* Page header — global component */}
      <PageHeader
        title={t("page.account.my_account")}
        subtitle={t(
          "page.account.manage_your_profile_company_and_security_setting",
        )}
        actions={(
          <button
            className="btn-manor"
            onClick={handleSave}
            disabled={saving}
            style={{ opacity: saving ? 0.5 : 1 }}
          >
            {saving
              ? t("page.task_collections.saving")
              : t("page.account.save_changes")}
          </button>
        )}
      />

      {success && (
        <div
          style={{
            marginBottom: 16,
            padding: "10px 16px",
            borderRadius: 12,
            background: "rgba(79,156,132,0.08)",
            border: "1px solid rgba(79,156,132,0.2)",
            color: "#437f6b",
            fontSize: 13,
            fontWeight: 600,
          }}
        >
          {success}
        </div>
      )}
      {formError && (
        <div
          style={{
            marginBottom: 16,
            padding: "10px 16px",
            borderRadius: 12,
            background: "rgba(193,74,68,0.06)",
            border: "1px solid rgba(193,74,68,0.15)",
            color: "#c14a44",
            fontSize: 13,
            fontWeight: 600,
          }}
        >
          {formError}
        </div>
      )}

      <div
        className="account-layout"
        style={{
          display: "flex",
          gap: 22,
          alignItems: "flex-start",
        }}
      >
        <IDCard user={user} />

        {/* Right: Account settings */}
        <div
          className="account-main-column"
          style={{
            flex: 1,
            minWidth: 0,
            display: "flex",
            flexDirection: "column",
            gap: 18,
          }}
        >
          <div
            className="account-form-panel glass-panel"
            style={{
              padding: "32px 36px",
              borderRadius: 28,
            }}
          >
            <h2
              style={{
                fontSize: 18,
                fontWeight: 800,
                color: "#1c1917",
                margin: "0 0 20px",
              }}
            >
              {t("page.account.personal_information")}
            </h2>

            <div
              className="account-field-grid"
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))",
                gap: 16,
                maxWidth: 560,
              }}
            >
              <div>
                <label className="manor-label">
                  {t("page.account.first_name")}
                </label>
                <input
                  className="manor-input"
                  value={firstName}
                  onChange={(e) => setFirstName(e.target.value)}
                  placeholder={t("page.account.john")}
                />
              </div>
              <div>
                <label className="manor-label">
                  {t("page.account.last_name")}
                </label>
                <input
                  className="manor-input"
                  value={lastName}
                  onChange={(e) => setLastName(e.target.value)}
                  placeholder={t("page.account.doe")}
                />
              </div>
            </div>

            <div style={{ marginTop: 20 }}>
              <label className="manor-label" style={{ marginBottom: 10 }}>
                {t("page.account.company")}
              </label>
              <CompanyCard
                user={user}
                context={peopleContext}
                focusInviteToken={focusInviteToken}
              />
              <LeaveTeamSection user={user} context={peopleContext} />
            </div>

            <div style={{ maxWidth: 560, marginTop: 16 }}>
              <label className="manor-label">
                {t("page.account.email_address")}
              </label>
              <input
                className="manor-input"
                value={user.email}
                disabled
                style={{ opacity: 0.5, cursor: "not-allowed" }}
              />
            </div>

            <div
              className="account-field-grid"
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))",
                gap: 16,
                maxWidth: 560,
                marginTop: 16,
              }}
            >
              <div>
                <label className="manor-label">
                  {t("page.account.phone_number")}
                </label>
                <input
                  className="manor-input"
                  value={phone}
                  onChange={(e) => setPhone(e.target.value)}
                  placeholder="+1 (555) 000-0000"
                />
              </div>
              <div>
                <label className="manor-label">
                  {t("page.account.timezone")}
                </label>
                <Select
                  value={timezone}
                  onChange={setTimezone}
                  filterable
                  options={[
                    "UTC",
                    "America/New_York",
                    "America/Chicago",
                    "America/Denver",
                    "America/Los_Angeles",
                    "Europe/London",
                    "Europe/Paris",
                    "Europe/Berlin",
                    "Asia/Shanghai",
                    "Asia/Tokyo",
                    "Asia/Singapore",
                    "Asia/Dubai",
                    "Asia/Riyadh",
                    "Australia/Sydney",
                  ]}
                  placeholder={t("page.account.select_timezone")}
                />
              </div>
            </div>

            <DangerZoneSection user={user} />
          </div>
        </div>
      </div>
    </div>
  );
}

/* ─── Leave Team ─── */
function LeaveTeamSection({
  user,
  context,
}: {
  user: any;
  context?: PeopleContext | null;
}) {
  const queryClient = useQueryClient();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const activeMembership = context?.active_membership || null;
  const canLeave = Boolean(activeMembership?.can_leave);

  if (!canLeave) return null;

  const handleLeave = async () => {
    if (submitting) return;
    setSubmitting(true);
    setError("");
    try {
      const result = await api.people.leaveMembership(activeMembership!.entity_id);
      if (!result.access_token) {
        leaveSessionOrLogout();
        return;
      }
      await applyPeopleContextResult(result, queryClient);
      setConfirmOpen(false);
      setSubmitting(false);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to leave team");
      setSubmitting(false);
    }
  };

  return (
    <>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          flexWrap: "wrap",
          gap: 16,
          maxWidth: 560,
          marginTop: 10,
          padding: "12px 14px",
          borderRadius: 12,
          background: "rgba(250,250,249,0.72)",
          border: "1px solid rgba(28,25,23,0.06)",
        }}
      >
        <div style={{ flex: "1 1 240px", minWidth: 0 }}>
          <p
            style={{
              fontSize: 13,
              fontWeight: 700,
              color: "#44403c",
              margin: "0 0 2px",
            }}
          >
            {t("page.account.leave_team")}
          </p>
          <p style={{ fontSize: 12, color: "#a8a29e", margin: 0 }}>
            {t("page.account.leave_team_description")}
          </p>
        </div>
        <button
          className="btn-manor-secondary"
          onClick={() => {
            setConfirmOpen(true);
            setError("");
          }}
          style={{ flexShrink: 0, marginLeft: "auto" }}
        >
          {t("page.account.leave_team")}
        </button>
      </div>

      <Modal
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        title={t("page.account.leave_team")}
        footer={
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button
              className="btn-manor-secondary"
              onClick={() => setConfirmOpen(false)}
            >
              {t("action.cancel")}
            </button>
            <button
              className="btn-manor-danger"
              onClick={handleLeave}
              disabled={submitting}
              style={{ opacity: submitting ? 0.5 : 1 }}
            >
              {submitting ? t("page.login.please_wait") : t("page.account.confirm_leave_team")}
            </button>
          </div>
        }
      >
        <p style={{ fontSize: 14, color: "#57534e", lineHeight: 1.6 }}>
          {t("page.account.leave_team_confirm_description")}
        </p>
        {error && (
          <p style={{ fontSize: 13, color: "#c14a44", marginTop: 12 }}>
            {error}
          </p>
        )}
      </Modal>
    </>
  );
}

/* ─── Danger Zone — account deletion ─── */
function DangerZoneSection({ user }: { user: any }) {
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [typedEmail, setTypedEmail] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [graceDays, setGraceDays] = useState(30);

  useEffect(() => {
    let cancelled = false;
    api.auth
      .accountGraceDays?.()
      .then((r) => {
        if (!cancelled && r?.grace_days) setGraceDays(r.grace_days);
      })
      .catch(() => {
        /* fall back to 30 */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const matchEmail =
    typedEmail.trim().toLowerCase() === (user.email || "").toLowerCase();
  const isAdmin = user.role === "owner" || user.role === "admin";

  const handleDelete = async () => {
    if (!matchEmail || submitting) return;
    setSubmitting(true);
    setError("");
    try {
      await api.auth.deleteAccount();
      // Soft-deleted: clear local token, kick to login, surface restore tip there.
      localStorage.removeItem("manor_token");
      const url = new URL("/login", window.location.origin);
      url.searchParams.set("account_deleted", "1");
      window.location.href = url.toString();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Failed to delete account",
      );
      setSubmitting(false);
    }
  };

  return (
    <div
      style={{
        marginTop: 32,
        paddingTop: 28,
        borderTop: "1px solid rgba(236,200,197,0.6)",
      }}
    >
      <h3 className="manor-section-title" style={{ color: "#a23e38" }}>
        {t("page.account.danger_zone")}
      </h3>
      <p className="manor-section-subtitle">
        {t("page.account.move_your_account_to_trash_restorable_via_the_lo")}{" "}
        {graceDays} {t("page.account.days_then_permanently_deleted")}
        {isAdmin
          ? t(
              "page.account.if_you_re_the_only_admin_in_this_organization_the_enti",
            )
          : t(
              "page.account.tasks_and_conversations_you_created_stay_in_the_worksp",
            )}
      </p>
      <button
        className="btn-manor-danger"
        onClick={() => {
          setConfirmOpen(true);
          setTypedEmail("");
          setError("");
        }}
        style={{ marginTop: 12 }}
      >
        {t("page.account.delete_my_account")}
      </button>

      <Modal
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        title={t("page.account.delete_account")}
        footer={
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button
              className="btn-manor-secondary"
              onClick={() => setConfirmOpen(false)}
            >
              {t("action.cancel")}
            </button>
            <button
              className="btn-manor-danger"
              onClick={handleDelete}
              disabled={!matchEmail || submitting}
              style={{ opacity: !matchEmail || submitting ? 0.5 : 1 }}
            >
              {submitting
                ? t("page.task_collections.deleting")
                : `Move to trash (${graceDays} day grace)`}
            </button>
          </div>
        }
      >
        <p style={{ fontSize: 14, color: "#57534e", lineHeight: 1.6 }}>
          {t("page.account.this_soft_deletes_your_account_you_can_sign_in_a")}{" "}
          {graceDays}{" "}
          {t("page.account.days_to_recover_it_after_that_your_account_is_pe")}
        </p>
        {isAdmin && (
          <p
            style={{
              fontSize: 13,
              color: "#936027",
              marginTop: 12,
              padding: 12,
              background: "#f3ecd6",
              borderRadius: 8,
            }}
          >
            {t("page.account.as_the")} {user.role}
            {t("page.account.your_organization_workspaces_agents_memory_billi")}
          </p>
        )}
        <div style={{ marginTop: 16 }}>
          <label className="manor-label">
            {t("page.account.type_your_email_to_confirm")}{" "}
            <strong>{user.email}</strong>
          </label>
          <input
            className="manor-input"
            value={typedEmail}
            onChange={(e) => setTypedEmail(e.target.value)}
            placeholder={user.email}
            autoComplete="off"
          />
        </div>
        {error && (
          <p style={{ fontSize: 13, color: "#c14a44", marginTop: 12 }}>
            {error}
          </p>
        )}
      </Modal>
    </div>
  );
}
