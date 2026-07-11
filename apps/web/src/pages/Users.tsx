import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { t } from "../lib/i18n";
import type { User } from "../lib/types";
import PageHeader, { PageHeaderAddButton } from "../components/ui/PageHeader";
import StatusBadge from "../components/ui/StatusBadge";
import SmartToolbar from "../components/ui/SmartToolbar";
import Modal from "../components/ui/Modal";
import ConfirmDialog from "../components/ui/ConfirmDialog";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import Button from "../components/ui/Button";
import Input from "../components/ui/Input";

const ROLE_BADGE: Record<string, { type: "teal" | "info" | "active" | "inactive"; labelKey: string }> = {
  owner: { type: "teal", labelKey: "page.users.role_owner" },
  admin: { type: "info", labelKey: "page.users.role_admin" },
  member: { type: "active", labelKey: "page.users.role_member" },
  viewer: { type: "inactive", labelKey: "page.users.role_viewer" },
};

const STATUS_MAP: Record<string, { type: "active" | "warning" | "inactive"; pulse: boolean }> = {
  active: { type: "active", pulse: true },
  invited: { type: "warning", pulse: false },
  inactive: { type: "inactive", pulse: false },
};

/* ── Avatar colours ── */
const AVATAR_GRADIENTS = [
  { from: "#e5eeeb", to: "#ccded9", fg: "#436b65" },
  { from: "#e3e9f1", to: "#bfdbfe", fg: "#3f57a0" },
  { from: "#f3e5ed", to: "#fbcfe8", fg: "#be185d" },
  { from: "#ece9f5", to: "#ddd6fe", fg: "#6443a0" },
  { from: "#f3ecd6", to: "#ecdca4", fg: "#936027" },
  { from: "#dceae3", to: "#c4dfd2", fg: "#3f7361" },
  { from: "#e8eff4", to: "#bae6fd", fg: "#426c87" },
  { from: "#f1dddb", to: "#ecc8c5", fg: "#a23e38" },
];

function getAvatarColor(name: string) {
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash);
  return AVATAR_GRADIENTS[Math.abs(hash) % AVATAR_GRADIENTS.length];
}

function UserAvatar({ name, size = 36 }: { name: string; size?: number }) {
  const c = getAvatarColor(name);
  return (
    <span
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        width: size,
        height: size,
        minWidth: size,
        minHeight: size,
        borderRadius: "50%",
        background: `linear-gradient(135deg, ${c.from}, ${c.to})`,
        color: c.fg,
        fontSize: size * 0.38,
        fontWeight: 800,
        userSelect: "none",
        flexShrink: 0,
      }}
    >
      {(name || "?").charAt(0).toUpperCase()}
    </span>
  );
}

export default function Users() {
  const queryClient = useQueryClient();
  const [showInvite, setShowInvite] = useState(false);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState("member");
  const [confirmDeactivate, setConfirmDeactivate] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [roleFilter, setRoleFilter] = useState("");

  const { data: users, isLoading } = useQuery({
    queryKey: ["users"],
    queryFn: () => api.users.list(),
  });

  const inviteMutation = useMutation({
    mutationFn: ({ email, role }: { email: string; role: string }) =>
      api.users.invite(email, role),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["users"] });
      setShowInvite(false);
      setInviteEmail("");
      setInviteRole("member");
    },
  });

  const roleMutation = useMutation({
    mutationFn: ({ userId, role }: { userId: string; role: string }) =>
      api.users.updateRole(userId, role),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["users"] }),
  });

  const deactivateMutation = useMutation({
    mutationFn: (userId: string) => api.users.deactivate(userId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["users"] });
      setConfirmDeactivate(null);
    },
  });

  const filtered = (users || []).filter((u: User) => {
    if (roleFilter && u.role !== roleFilter) return false;
    if (!search) return true;
    const q = search.toLowerCase();
    return (
      (u.display_name || u.email || "").toLowerCase().includes(q) ||
      (u.email || "").toLowerCase().includes(q)
    );
  });

  const totalUsers = (users || []).length;
  const activeUsers = (users || []).filter((u: User) => u.status === "active").length;

  if (isLoading) {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", gap: 12, color: "#a8a29e" }}>
        <LoadingSpinner size={20} />
        <span style={{ fontSize: 14 }}>{t("page.users.loading")}</span>
      </div>
    );
  }

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", padding: "1rem", overflow: "hidden", position: "relative", zIndex: 10 }}>
      {/* Header */}
      <PageHeader
        title={t("nav.users")}
        subtitle={`${activeUsers} ${t("page.users.active")} / ${totalUsers} ${t("page.users.total")}`}
        toolbar={(
          <SmartToolbar
            searchValue={search}
            onSearchChange={setSearch}
            searchPlaceholder={t("page.users.search_placeholder")}
            filterOptions={[
              { key: "", label: t("page.users.all_roles") },
              { key: "owner", label: t("page.users.role_owner") },
              { key: "admin", label: t("page.users.role_admin") },
              { key: "member", label: t("page.users.role_member") },
              { key: "viewer", label: t("page.users.role_viewer") },
            ]}
            filterValue={roleFilter}
            onFilterChange={setRoleFilter}
            className="w-full sm:w-64"
          />
        )}
        actions={<PageHeaderAddButton label={t("page.users.add_user")} onClick={() => setShowInvite(true)} />}
      />

      {/* Invite Modal */}
      <Modal
        open={showInvite}
        onClose={() => { setShowInvite(false); setInviteEmail(""); setInviteRole("member"); }}
        title={t("page.users.invite_user")}
        footer={
          <>
            <Button
              variant="outline"
              onClick={() => { setShowInvite(false); setInviteEmail(""); setInviteRole("member"); }}
            >
              {t("action.cancel")}
            </Button>
            <Button
              variant="primary"
              disabled={!inviteEmail || inviteMutation.isPending}
              onClick={() => inviteMutation.mutate({ email: inviteEmail, role: inviteRole })}
            >
              {inviteMutation.isPending ? t("page.users.inviting") : t("page.users.send_invite")}
            </Button>
          </>
        }
      >
        <div className="space-y-4">
          <Input
            label={t("page.users.email")}
            type="email"
            value={inviteEmail}
            onChange={(e) => setInviteEmail(e.target.value)}
            placeholder={t("page.users.user_example_com")}
          />
          <div>
            <label className="block text-sm font-medium text-stone-600 mb-1">{t("page.users.role")}</label>
            <select
              value={inviteRole}
              onChange={(e) => setInviteRole(e.target.value)}
              className="manor-input"
            >
              <option value="viewer">{t("page.users.role_viewer")}</option>
              <option value="member">{t("page.users.role_member")}</option>
              <option value="admin">{t("page.users.role_admin")}</option>
            </select>
          </div>
          {inviteMutation.isError && (
            <p className="text-red-600 text-sm">{(inviteMutation.error as Error).message}</p>
          )}
        </div>
      </Modal>

      {/* Deactivate Confirmation */}
      <ConfirmDialog
        open={!!confirmDeactivate}
        onClose={() => setConfirmDeactivate(null)}
        onConfirm={() => { if (confirmDeactivate) deactivateMutation.mutate(confirmDeactivate); }}
        title={t("page.users.deactivate_user")}
        message={t("page.users.deactivate_message")}
        confirmLabel={deactivateMutation.isPending ? t("page.users.deactivating") : t("page.users.deactivate")}
        danger
      />

      {/* Users Table */}
      <div style={{ flex: 1, overflowY: "auto", padding: "8px" }}>
        {filtered.length > 0 ? (
          <div className="glass-panel" style={{ overflow: "hidden", padding: "8px 16px 16px" }}>
            <table className="glass-table">
              <thead>
                <tr>
                  <th>{t("page.users.user")}</th>
                  <th>{t("page.users.email")}</th>
                  <th>{t("page.users.role")}</th>
                  <th>{t("page.users.status")}</th>
                  <th style={{ textAlign: "right" }}>{t("page.users.actions")}</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((u: User) => {
                  const roleBadge = ROLE_BADGE[u.role] || ROLE_BADGE.member;
                  const statusCfg = STATUS_MAP[u.status] || STATUS_MAP.inactive;
                  return (
                    <tr key={u.id}>
                      <td>
                        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                          <UserAvatar name={u.display_name || u.email || "U"} size={36} />
                          <span style={{ fontSize: 14, fontWeight: 600, color: "#292524" }}>
                            {u.display_name || u.email}
                          </span>
                        </div>
                      </td>
                      <td>{u.email}</td>
                      <td>
                        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                          <StatusBadge type={roleBadge.type}>{t(roleBadge.labelKey)}</StatusBadge>
                          <select
                            value={u.role}
                            onChange={(e) => roleMutation.mutate({ userId: u.id, role: e.target.value })}
                            style={{
                              padding: "4px 8px",
                              borderRadius: 8,
                              fontSize: 12,
                              fontWeight: 500,
                              color: "#57534e",
                              background: "#fafaf9",
                              border: "1px solid rgba(28,25,23,0.06)",
                              cursor: "pointer",
                              outline: "none",
                              transition: "border-color 0.15s",
                            }}
                          >
                            <option value="viewer">{t("page.users.role_viewer")}</option>
                            <option value="member">{t("page.users.role_member")}</option>
                            <option value="admin">{t("page.users.role_admin")}</option>
                            <option value="owner">{t("page.users.role_owner")}</option>
                          </select>
                        </div>
                      </td>
                      <td>
                        <StatusBadge type={statusCfg.type} dot pulse={statusCfg.pulse}>
                          {u.status}
                        </StatusBadge>
                      </td>
                      <td style={{ textAlign: "right" }}>
                        {u.status !== "inactive" && (
                          <button
                            onClick={() => setConfirmDeactivate(u.id)}
                            style={{
                              padding: "4px 12px",
                              borderRadius: 8,
                              fontSize: 12,
                              fontWeight: 600,
                              color: "#d65f59",
                              background: "transparent",
                              border: "1px solid transparent",
                              cursor: "pointer",
                              transition: "all 0.15s",
                            }}
                            onMouseEnter={(e) => { (e.target as HTMLElement).style.background = "#f8f0ef"; }}
                            onMouseLeave={(e) => { (e.target as HTMLElement).style.background = "transparent"; }}
                          >
                            {t("page.users.deactivate")}
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState
            icon={
              <svg style={{ width: 32, height: 32, color: "#d6d3d1" }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M15 19.128a9.38 9.38 0 002.625.372 9.337 9.337 0 004.121-.952 4.125 4.125 0 00-7.533-2.493M15 19.128v-.003c0-1.113-.285-2.16-.786-3.07M15 19.128v.106A12.318 12.318 0 018.624 21c-2.331 0-4.512-.645-6.374-1.766l-.001-.109a6.375 6.375 0 0111.964-3.07M12 6.375a3.375 3.375 0 11-6.75 0 3.375 3.375 0 016.75 0zm8.25 2.25a2.625 2.625 0 11-5.25 0 2.625 2.625 0 015.25 0z" />
              </svg>
            }
            title={search || roleFilter ? t("page.users.no_users_match") : t("page.users.no_users")}
            description={search || roleFilter ? t("page.users.try_adjusting") : t("page.users.invite_first_desc")}
            action={
              !(search || roleFilter) ? (
                <Button variant="primary" size="sm" onClick={() => setShowInvite(true)}>{t("page.users.invite_user")}</Button>
              ) : undefined
            }
          />
        )}
      </div>
    </div>
  );
}
