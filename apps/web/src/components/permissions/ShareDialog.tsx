/**
 * ShareDialog — Google-Drive-style single-screen layout with proper
 * enterprise people-picker.
 *
 * Three vertical sections in one view (no tabs):
 *
 *   1. Add people   — <PeoplePicker> (typeahead from team roster) +
 *                     role select + "Notify" checkbox
 *   2. People with access — flat list of <PersonRow> (in-place role change,
 *                          trash to remove; workspace-inherited rows locked)
 *   3. General access — Restricted / Anyone with link / @domain
 *                       + Copy link button (always visible)
 *
 * What's "enterprise-grade" here:
 *   - You pick from real team members (api.staff.list), not type free emails.
 *   - Each member shows avatar + email + department + title for disambiguation.
 *   - Free email entry is still allowed (as last-resort "invite external")
 *     but visually marked so users notice.
 *   - Role dropdown describes what each role can do (Viewer = 只读 etc).
 *   - Workspace-inherited grants are visibly distinct + locked so users
 *     understand they need to manage them at the workspace level.
 *   - Classification + visibility stay as passive labels (no badges
 *     elsewhere — cloud-drive principle).
 */
import { useEffect, useMemo, useRef, useState } from "react";
import type { Classification, Visibility } from "../../lib/types";
import { t } from "../../lib/i18n";
import { translateApiError } from "../../lib/api";
import Modal from "../ui/Modal";
import Button from "../ui/Button";
import Select from "../ui/Select";
import Input from "../ui/Input";
import Textarea from "../ui/Textarea";
import Toggle from "../ui/Toggle";
import UserAvatar from "../ui/UserAvatar";
import { IconLink } from "../icons";
import ClassificationBadge from "./ClassificationBadge";
import VisibilityIcon from "./VisibilityIcon";
import PeoplePicker, { type StaffOption } from "./PeoplePicker";
import PersonRow, { type ShareRole } from "./PersonRow";

// ── Local types ──────────────────────────────────────────────────────────

interface InternalGrant {
  id: string;
  user_email: string;
  user_name?: string;
  avatar_url?: string | null;
  role: ShareRole;
  expires_at?: string;
  source: "explicit" | "workspace";
  /** Optional staff id, when grant was added via the picker. Used to
   *  exclude that staff from subsequent picker results. */
  staff_id?: string;
}

interface ExternalShare {
  id: string;
  audience: string;
  capabilities: string[];
  expires_at?: string;
  watermark: boolean;
  require_otp: boolean;
  use_count: number;
  last_used_at?: string;
  url?: string;
}

export type GeneralAccessMode = "restricted" | "anyone_link" | "domain";

interface Props {
  open: boolean;
  onClose: () => void;
  resourceType: "document" | "document_folder" | "task";
  resourceId: string;
  resourceName: string;
  classification?: Classification;
  visibility?: Visibility;
  internalGrants?: InternalGrant[];
  externalShares?: ExternalShare[];
  /** Caller-derived entity domain — enables the "Anyone @<domain>" option. */
  entityDomain?: string;
  onAddInternal?: (
    pick:
      | { kind: "staff"; staff: StaffOption }
      | { kind: "external_email"; email: string },
    role: ShareRole,
    opts: { expiresAt?: string; notify: boolean; message?: string },
  ) => Promise<void>;
  onUpdateInternalRole?: (grantId: string, role: ShareRole) => Promise<void>;
  onRemoveInternal?: (grantId: string) => Promise<void>;
  onCreateExternal?: (config: NewExternalShareConfig) => Promise<{ url?: string; pending?: boolean }>;
  onRevokeExternal?: (shareId: string) => Promise<void>;
  externalShareNeedsApproval?: boolean;
}

export interface NewExternalShareConfig {
  audience_type: "anonymous" | "email" | "domain";
  audience_value?: string;
  capabilities: ("view" | "comment" | "download")[];
  expires_in_days: number;
  watermark: boolean;
  require_otp: boolean;
  approval_reason?: string;
}

function _roleOptions(): { value: ShareRole; label: string }[] {
  return [
    { value: "viewer", label: t("permissions.role.viewer.label") },
    { value: "commenter", label: t("permissions.role.commenter.label") },
    { value: "editor", label: t("permissions.role.editor.label") },
    { value: "curator", label: t("permissions.role.curator.label") },
  ];
}

const ANON_ROLES = [
  { value: "view", label: "Viewer" },
  { value: "comment", label: "Commenter" },
  { value: "download", label: "Downloader" },
] as const;

function _anonRoleOptions(): { value: string; label: string }[] {
  return [
    { value: "view", label: t("permissions.role.viewer.label") },
    { value: "comment", label: t("permissions.role.commenter.label") },
    { value: "download", label: t("permissions.role.downloader.label") },
  ];
}

type AnonRole = (typeof ANON_ROLES)[number]["value"];

function _anonRoleToCaps(role: AnonRole): ("view" | "comment" | "download")[] {
  if (role === "comment") return ["view", "comment"];
  if (role === "download") return ["view", "download"];
  return ["view"];
}

function _capsToAnonRole(caps: string[]): AnonRole {
  if (caps.includes("download")) return "download";
  if (caps.includes("comment")) return "comment";
  return "view";
}

// ── Component ────────────────────────────────────────────────────────────

export default function ShareDialog({
  open,
  onClose,
  resourceName,
  classification,
  visibility,
  internalGrants = [],
  externalShares = [],
  entityDomain,
  onAddInternal,
  onUpdateInternalRole,
  onRemoveInternal,
  onCreateExternal,
  onRevokeExternal,
  externalShareNeedsApproval = false,
}: Props) {
  // ── Find the active "general" share (anonymous or domain) ──
  const generalShare = useMemo(
    () =>
      externalShares.find(
        (s) => s.audience === "anonymous" || (s.audience || "").startsWith("domain:"),
      ),
    [externalShares],
  );

  const initialMode: GeneralAccessMode = useMemo(() => {
    if (!generalShare) return "restricted";
    if (generalShare.audience === "anonymous") return "anyone_link";
    if ((generalShare.audience || "").startsWith("domain:")) return "domain";
    return "restricted";
  }, [generalShare]);

  const initialAnonRole: AnonRole = useMemo(
    () => (generalShare ? _capsToAnonRole(generalShare.capabilities) : "view"),
    [generalShare],
  );

  const [mode, setMode] = useState<GeneralAccessMode>(initialMode);
  const [anonRole, setAnonRole] = useState<AnonRole>(initialAnonRole);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingNote, setPendingNote] = useState<string | null>(null);
  const [linkCopied, setLinkCopied] = useState(false);
  // Session-cached URL from the most recent ``onCreateExternal`` call. The
  // backend only returns the raw token (and therefore the public URL) at
  // creation time — listShares never re-emits it because tokens are stored
  // as sha256 hashes. Without this cache, refetching ``sharesQuery`` after
  // create wipes the URL and the Copy link button stays disabled forever.
  const [lastCreatedUrl, setLastCreatedUrl] = useState<string | null>(null);

  // Reset session state on the open→true *transition* only.
  //
  // Previously this effect depended on [open, initialMode, initialAnonRole]
  // — but `initialMode` is derived from `generalShare` (an externalShares
  // memo), so AFTER a successful share create the sharesQuery refetch
  // populates externalShares → generalShare flips truthy → initialMode
  // recomputes from "restricted" to "anyone_link" → effect re-runs
  // mid-session → ``setLastCreatedUrl(null)`` wipes the URL we just
  // cached in `applyGeneralAccess`. Net result: Copy link button never
  // activates even though POST returned 201.
  //
  // Track previous open via a ref so the reset is gated on the actual
  // dialog open transition, not on any prop that happens to change while
  // the dialog is up.
  const prevOpenRef = useRef(false);
  useEffect(() => {
    const wasOpen = prevOpenRef.current;
    prevOpenRef.current = open;
    if (open && !wasOpen) {
      setMode(initialMode);
      setAnonRole(initialAnonRole);
      setError(null);
      setPendingNote(null);
      setLinkCopied(false);
      setLastCreatedUrl(null);
    }
  }, [open, initialMode, initialAnonRole]);

  const isRestrictedDoc = classification === "restricted";
  // Prefer the prop URL when it exists (rare — only the create response
  // carries it), otherwise use the session cache. We deliberately don't
  // gate on `generalShare` being defined here: the sharesQuery refetch
  // after create is async, so during the window between
  // `onCreateExternal` returning and the list query landing,
  // `generalShare` is still undefined — yet we already know the URL from
  // the create response. Using `lastCreatedUrl` regardless keeps the
  // Copy link button enabled the moment the share exists. Hygiene:
  // ``applyGeneralAccess`` clears ``lastCreatedUrl`` whenever the user
  // switches back to ``restricted`` (which also revokes the share), so
  // we never show a stale URL for a no-longer-active share.
  const liveUrl = generalShare?.url ?? lastCreatedUrl;

  const copyLink = async () => {
    if (!liveUrl) return;
    try {
      await navigator.clipboard.writeText(liveUrl);
      setLinkCopied(true);
      setTimeout(() => setLinkCopied(false), 1800);
    } catch {
      // ignore — best effort
    }
  };

  async function applyGeneralAccess(nextMode: GeneralAccessMode, nextRole: AnonRole) {
    if (isRestrictedDoc && nextMode !== "restricted") return;
    setBusy(true);
    setError(null);
    setPendingNote(null);
    try {
      if (generalShare && onRevokeExternal) {
        await onRevokeExternal(generalShare.id);
      }
      if (nextMode === "restricted") {
        // No active share anymore — drop the cached URL so Copy link
        // (gated on liveUrl) goes back to disabled, and we don't briefly
        // show a stale URL during the next mode toggle before
        // sharesQuery refetches.
        setLastCreatedUrl(null);
        return;
      }
      if (!onCreateExternal) return;
      const audience_type = nextMode === "domain" ? "domain" : "anonymous";
      const audience_value = nextMode === "domain" ? entityDomain : undefined;
      if (nextMode === "domain" && !audience_value) {
        setError(t("permissions.error.no_entity_domain"));
        return;
      }
      const config: NewExternalShareConfig = {
        audience_type,
        audience_value,
        capabilities: _anonRoleToCaps(nextRole),
        expires_in_days: 7,
        watermark: true,
        require_otp: false,
        approval_reason: externalShareNeedsApproval
          ? `General access -> ${nextMode === "domain" ? `@${audience_value}` : "anyone with link"} (${nextRole})`
          : undefined,
      };
      // Reset any prior cached URL — the new share has a different token.
      setLastCreatedUrl(null);
      const result = await onCreateExternal(config);
      if (result.pending) {
        setPendingNote(t("permissions.share.approval_pending"));
      }
      // Cache the URL for the modal session — sharesQuery refetch will
      // surface a matching ``generalShare`` row without ``url``, so this
      // cache is what keeps the Copy link button enabled.
      if (result.url) setLastCreatedUrl(result.url);
    } catch (err: any) {
      setError(translateApiError(err, t("permissions.error.generic")));
      setMode(initialMode);
      setAnonRole(initialAnonRole);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={open} onClose={onClose} title={`${t("permissions.share.title_prefix")} ${resourceName}`} maxWidth="540px">
      {/* Header summary — passive labels */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          marginBottom: 20,
          flexWrap: "wrap",
        }}
      >
        <ClassificationBadge level={classification} size="sm" />
        <span
          style={{
            fontSize: 12,
            color: "#a8a29e",
            display: "inline-flex",
            alignItems: "center",
            gap: 4,
          }}
        >
          <VisibilityIcon visibility={visibility} size={12} />
          {visibility ?? "—"}
        </span>
      </div>

      {/* ── Section 1: Add people ──────────────────────────────────────── */}
      <AddPeopleSection
        onSubmit={onAddInternal}
        excludeStaffIds={internalGrants
          .map((g) => g.staff_id)
          .filter((x): x is string => !!x)}
        onError={(msg) => setError(msg)}
      />

      {/* ── Section 2: People with access ──────────────────────────────── */}
      <PeopleWithAccessSection
        grants={internalGrants}
        onUpdateRole={onUpdateInternalRole}
        onRemove={onRemoveInternal}
        onError={(msg) => setError(msg)}
      />

      {/* ── Section 3: General access ──────────────────────────────────── */}
      <GeneralAccessSection
        mode={mode}
        anonRole={anonRole}
        isRestrictedDoc={isRestrictedDoc}
        confidentialApproval={externalShareNeedsApproval}
        entityDomain={entityDomain}
        busy={busy}
        onModeChange={(m) => {
          setMode(m);
          applyGeneralAccess(m, anonRole);
        }}
        onRoleChange={(r) => {
          setAnonRole(r);
          if (mode !== "restricted") applyGeneralAccess(mode, r);
        }}
      />

      {/* Status row */}
      {(error || pendingNote) && (
        <div
          style={{
            marginTop: 12,
            padding: "10px 12px",
            borderRadius: 6,
            fontSize: 12,
            lineHeight: 1.4,
            background: error ? "#f8f0ef" : "#f9f4ec",
            color: error ? "#a23e38" : "#7c4a2e",
          }}
        >
          {error || pendingNote}
        </div>
      )}

      {/* Footer — smart link button on the left, Done on the right.
          State machine:
            mode=restricted               → button disabled, no hint
            mode=anyone_link, liveUrl=set → "Copy link" (copies to clipboard)
            mode=anyone_link, no liveUrl, generalShare exists
                                          → "Generate link" — clicking
                                            revokes the unrecoverable hashed-
                                            token share + creates a fresh
                                            one. This is the path users
                                            hit when they reopen the dialog
                                            after a previous session: the
                                            URL is unrecoverable from DB
                                            since only sha256 is stored.
            mode=anyone_link, no liveUrl, no share yet (busy)
                                          → button shows "Copy link"
                                            disabled + "Generating link…"
                                            hint while applyGeneralAccess
                                            is in flight. */}
      {(() => {
        const linkActive = mode !== "restricted";
        const canCopy = linkActive && !!liveUrl;
        const needsRegenerate = linkActive && !liveUrl && !!generalShare;
        const hint =
          linkActive && !liveUrl && busy
            ? t("permissions.share.footer.creating_link")
            : linkActive && !liveUrl && generalShare
              ? t("permissions.share.footer.regenerate_hint")
              : "";
        return (
          <div
            style={{
              marginTop: 18,
              paddingTop: 14,
              borderTop: "1px solid rgba(28,25,23,0.06)",
              display: "flex",
              flexDirection: "column",
              gap: 10,
            }}
          >
            {/* Hint sits on its OWN row so longer copy ("Previous URL
                can't be recovered…") wraps cleanly instead of getting
                truncated when squeezed between the two buttons. */}
            {hint && (
              <p
                style={{
                  margin: 0,
                  fontSize: 11,
                  color: "#a8a29e",
                  lineHeight: 1.5,
                }}
              >
                {hint}
              </p>
            )}
            {/* URL preview: when we have a live URL we surface it as a
                read-only text field so users can SEE the link, not just
                trust that "Copy link" silently put something in their
                clipboard. Auto-selects on click for paste-into-anywhere
                workflows. */}
            {canCopy && liveUrl && (
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  padding: "8px 10px",
                  borderRadius: 8,
                  background: "rgba(79,125,117,0.06)",
                  border: "1px solid rgba(79,125,117,0.25)",
                }}
              >
                <input
                  type="text"
                  value={liveUrl}
                  readOnly
                  onFocus={(e) => e.currentTarget.select()}
                  onClick={(e) => e.currentTarget.select()}
                  style={{
                    flex: 1,
                    minWidth: 0,
                    border: "none",
                    background: "transparent",
                    fontFamily:
                      "ui-monospace, SFMono-Regular, Menlo, monospace",
                    fontSize: 12,
                    color: "#436b65",
                    outline: "none",
                  }}
                />
              </div>
            )}
            <div
              style={{
                display: "flex",
                gap: 12,
                alignItems: "center",
                justifyContent: "space-between",
              }}
            >
              <div style={{ flexShrink: 0 }}>
                {canCopy ? (
                  <Button variant="outline" onClick={copyLink} disabled={busy}>
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, whiteSpace: "nowrap" }}>
                      <IconLink size={14} />
                      {linkCopied ? t("permissions.action.copied") : t("permissions.action.copy_link")}
                    </span>
                  </Button>
                ) : (
                  <Button
                    variant="outline"
                    onClick={() => applyGeneralAccess(mode, anonRole)}
                    disabled={!linkActive || busy}
                  >
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, whiteSpace: "nowrap" }}>
                      <IconLink size={14} />
                      {needsRegenerate
                        ? t("permissions.action.regenerate_link")
                        : t("permissions.action.copy_link")}
                    </span>
                  </Button>
                )}
              </div>
              <div style={{ flexShrink: 0 }}>
                <Button variant="primary" onClick={onClose}>
                  <span style={{ whiteSpace: "nowrap", padding: "0 4px" }}>
                    {t("permissions.action.done")}
                  </span>
                </Button>
              </div>
            </div>
          </div>
        );
      })()}
    </Modal>
  );
}

// ── Section 1: Add people ────────────────────────────────────────────────

function AddPeopleSection({
  onSubmit,
  excludeStaffIds,
  onError,
}: {
  onSubmit?: Props["onAddInternal"];
  excludeStaffIds: string[];
  onError: (msg: string) => void;
}) {
  const [pending, setPending] = useState<
    | { kind: "staff"; staff: StaffOption }
    | { kind: "external_email"; email: string }
    | null
  >(null);
  const [role, setRole] = useState<ShareRole>("viewer");
  const [notify, setNotify] = useState(true);
  const [message, setMessage] = useState("");
  const [expiresAt, setExpiresAt] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!onSubmit || !pending) return;
    setBusy(true);
    try {
      await onSubmit(pending, role, {
        expiresAt: expiresAt || undefined,
        notify,
        message: notify && message.trim() ? message.trim() : undefined,
      });
      setPending(null);
      setMessage("");
      setExpiresAt("");
    } catch (e: any) {
      onError(translateApiError(e, t("permissions.error.add_failed")));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section style={{ marginBottom: 22 }}>
      <SectionLabel>{t("permissions.share.section.add_people")}</SectionLabel>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 120px auto",
          gap: 6,
          alignItems: "center",
          marginTop: 8,
        }}
      >
        {pending ? (
          <PendingChip
            label={
              pending.kind === "staff"
                ? pending.staff.name
                : pending.email
            }
            sub={
              pending.kind === "staff"
                ? pending.staff.email ?? ""
                : t("permissions.share.external_email_label")
            }
            avatarUrl={pending.kind === "staff" ? pending.staff.avatar_url : null}
            onClear={() => setPending(null)}
          />
        ) : (
          <PeoplePicker
            onPick={setPending}
            excludeStaffIds={excludeStaffIds}
          />
        )}
        <Select
          value={role}
          onChange={(v) => setRole(v as ShareRole)}
          options={_roleOptions()}
        />
        <Button variant="primary" onClick={submit} disabled={!pending || busy}>
          {t("permissions.action.add")}
        </Button>
      </div>

      <div
        style={{
          marginTop: 10,
          display: "flex",
          flexWrap: "wrap",
          gap: 14,
          alignItems: "center",
        }}
      >
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 8,
            fontSize: 12,
            color: "#57534e",
          }}
        >
          <Toggle
            checked={notify}
            onChange={() => setNotify(!notify)}
            size="sm"
            aria-label={t("permissions.share.notify_label")}
          />
          {t("permissions.share.notify_label")}
        </span>
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            fontSize: 11,
            color: "#a8a29e",
            flex: 1,
            minWidth: 180,
          }}
        >
          <span style={{ whiteSpace: "nowrap" }}>{t("permissions.share.expires_label")}:</span>
          <div style={{ flex: 1, minWidth: 120 }}>
            <Input
              type="date"
              value={expiresAt}
              onChange={(e) => setExpiresAt(e.target.value)}
            />
          </div>
        </span>
      </div>
      {notify && (
        <div style={{ marginTop: 8 }}>
          <Textarea
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            placeholder={t("permissions.share.message_placeholder")}
            rows={2}
          />
        </div>
      )}
    </section>
  );
}

function PendingChip({
  label,
  sub,
  avatarUrl,
  onClear,
}: {
  label: string;
  sub: string;
  avatarUrl?: string | null;
  onClear: () => void;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        padding: "6px 10px",
        border: "1px solid rgba(67,107,101,0.3)",
        background: "rgba(67,107,101,0.06)",
        borderRadius: 8,
      }}
    >
      <UserAvatar name={label} avatarUrl={avatarUrl ?? null} size={28} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            fontSize: 13,
            fontWeight: 600,
            color: "#436b65",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {label}
        </div>
        <div
          style={{
            fontSize: 11,
            color: "#a8a29e",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {sub}
        </div>
      </div>
      <button
        type="button"
        onClick={onClear}
        aria-label={t("permissions.action.clear")}
        style={{
          width: 22,
          height: 22,
          borderRadius: 6,
          background: "transparent",
          border: "none",
          cursor: "pointer",
          color: "#436b65",
          fontSize: 16,
          lineHeight: 1,
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          flexShrink: 0,
        }}
      >
        ×
      </button>
    </div>
  );
}

// ── Section 2: People with access ────────────────────────────────────────

function PeopleWithAccessSection({
  grants,
  onUpdateRole,
  onRemove,
  onError,
}: {
  grants: InternalGrant[];
  onUpdateRole?: Props["onUpdateInternalRole"];
  onRemove?: Props["onRemoveInternal"];
  onError: (msg: string) => void;
}) {
  const [busyRow, setBusyRow] = useState<string | null>(null);

  const sorted = useMemo(() => {
    return [...grants].sort((a, b) => {
      if (a.source === b.source) return a.user_email.localeCompare(b.user_email);
      return a.source === "explicit" ? -1 : 1;
    });
  }, [grants]);

  const changeRole = async (g: InternalGrant, nextRole: ShareRole) => {
    if (!onUpdateRole) return;
    setBusyRow(g.id);
    try {
      await onUpdateRole(g.id, nextRole);
    } catch (e: any) {
      onError(translateApiError(e, t("permissions.error.update_role_failed")));
    } finally {
      setBusyRow(null);
    }
  };

  const remove = async (g: InternalGrant) => {
    if (!onRemove) return;
    setBusyRow(g.id);
    try {
      await onRemove(g.id);
    } catch (e: any) {
      onError(translateApiError(e, t("permissions.error.remove_failed")));
    } finally {
      setBusyRow(null);
    }
  };

  return (
    <section style={{ marginBottom: 22 }}>
      <SectionLabel>{t("permissions.share.section.people_with_access", { count: sorted.length })}</SectionLabel>
      {sorted.length === 0 ? (
        <p
          style={{
            fontSize: 12,
            color: "#a8a29e",
            margin: "10px 0 0",
            padding: "12px 0",
            textAlign: "center",
            background: "rgba(245,245,244,0.4)",
            borderRadius: 6,
          }}
        >
          {t("permissions.share.empty_list")}
        </p>
      ) : (
        <div
          style={{
            marginTop: 8,
            display: "flex",
            flexDirection: "column",
            gap: 2,
          }}
        >
          {sorted.map((g) => (
            <PersonRow
              key={g.id}
              name={g.user_name || g.user_email}
              email={g.user_email}
              avatarUrl={g.avatar_url}
              role={g.role}
              source={g.source}
              expiresAt={g.expires_at}
              busy={busyRow === g.id}
              onRoleChange={(r) => changeRole(g, r)}
              onRemove={() => remove(g)}
            />
          ))}
        </div>
      )}
    </section>
  );
}

// ── Section 3: General access ────────────────────────────────────────────

function GeneralAccessSection({
  mode,
  anonRole,
  isRestrictedDoc,
  confidentialApproval,
  entityDomain,
  busy,
  onModeChange,
  onRoleChange,
}: {
  mode: GeneralAccessMode;
  anonRole: AnonRole;
  isRestrictedDoc: boolean;
  confidentialApproval: boolean;
  entityDomain?: string;
  busy: boolean;
  onModeChange: (m: GeneralAccessMode) => void;
  onRoleChange: (r: AnonRole) => void;
}) {
  const modeOptions: { value: GeneralAccessMode; label: string }[] = [
    { value: "restricted", label: t("permissions.share.general.restricted") },
    { value: "anyone_link", label: t("permissions.share.general.anyone_link") },
  ];
  if (entityDomain) {
    modeOptions.push({
      value: "domain",
      label: t("permissions.share.general.domain", { domain: entityDomain }),
    });
  }

  const description: string = (() => {
    if (isRestrictedDoc) return t("permissions.share.desc.restricted_doc");
    if (mode === "restricted") return t("permissions.share.desc.list_only");
    if (mode === "anyone_link")
      return confidentialApproval
        ? t("permissions.share.desc.anyone_link_approval")
        : t("permissions.share.desc.anyone_link");
    if (mode === "domain")
      return confidentialApproval
        ? t("permissions.share.desc.domain_approval", { domain: entityDomain ?? "" })
        : t("permissions.share.desc.domain", { domain: entityDomain ?? "" });
    return "";
  })();

  return (
    <section>
      <SectionLabel>{t("permissions.share.section.general_access")}</SectionLabel>
      <div
        style={{
          marginTop: 8,
          display: "grid",
          gridTemplateColumns: "1fr 120px",
          gap: 6,
        }}
      >
        <Select
          value={mode}
          onChange={(v) => onModeChange(v as GeneralAccessMode)}
          options={modeOptions}
        />
        {mode !== "restricted" && !isRestrictedDoc && (
          <Select
            value={anonRole}
            onChange={(v) => onRoleChange(v as AnonRole)}
            options={_anonRoleOptions()}
          />
        )}
      </div>
      <p
        style={{
          fontSize: 11,
          color: isRestrictedDoc ? "#a23e38" : "#a8a29e",
          margin: "8px 0 0",
          lineHeight: 1.5,
        }}
      >
        {description}
      </p>
      {busy && (
        <p style={{ fontSize: 11, color: "#a8a29e", margin: "6px 0 0" }}>
          {t("permissions.share.general.applying")}
        </p>
      )}
    </section>
  );
}

// ── Bits ─────────────────────────────────────────────────────────────────

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        fontSize: 11,
        fontWeight: 700,
        color: "#57534e",
        textTransform: "uppercase",
        letterSpacing: 0.4,
      }}
    >
      {children}
    </div>
  );
}
